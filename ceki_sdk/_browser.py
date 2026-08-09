from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, Literal, cast

import httpx

from .humanize import Humanizer, HumanProfile

if TYPE_CHECKING:
    from ._client import Client
from ._captcha import CaptchaResult
from ._exceptions import (
    CaptchaTimeoutError,
    CdpUnrecoverable,
    InsufficientFunds,
    ProviderDisconnected,
    RateLimitExceeded,
    SessionEnded,
)
from ._models import Match, Snapshot

log = logging.getLogger(__name__)

mimetypes.init()
for _ext, _mime in {".avif": "image/avif", ".webm": "video/webm", ".woff2": "font/woff2"}.items():
    if not mimetypes.guess_type(f"x{_ext}")[0]:
        mimetypes.add_type(_mime, _ext)

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
TabOpenedCallback = Callable[[str], Awaitable[None]]
SimpleCallback = Callable[[], Awaitable[None]]
UserEventCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]

_ERROR_TERMINAL = {-1011, -1012, -1015, -1018}

# task 4109 — anti-detect branching for Browser.type().
# When both gates pass, a long text-with-selector call routes through the
# real system-clipboard Ctrl+V path (from task 4098) instead of the per-key
# Ceki.typeText path. Perfect per-key rhythm on a long string is a classic
# bot signal; a paste event with inputType=insertFromPaste looks like the
# normal "user pasted from clipboard" behavior. Named constants live here
# (not inside the method) so tests can pin them and future tuning does not
# leave magic numbers in two places.
TYPE_PASTE_MIN_CHARS = 500
TYPE_PASTE_PROBABILITY = 0.625


def _resolve_human(human) -> Humanizer | None:
    if os.environ.get("CEKI_HUMAN_DISABLE", "").lower() in ("1", "true", "yes"):
        return None
    if human is None:
        return None
    if isinstance(human, HumanProfile):
        return Humanizer(human)
    if isinstance(human, dict):
        return Humanizer(HumanProfile.from_dict(human))
    if isinstance(human, (str, Path)):
        s = str(human)
        if s in ("natural", "careful"):
            return Humanizer(HumanProfile.load_preset(s))
        return Humanizer(HumanProfile.load(s))
    raise ValueError(f"Invalid human profile: {human!r}")


def _unwrap_screenshot_data(resp: Any) -> str:
    """Extract the base64 screenshot payload from a captureScreenshot response.

    Some extension versions wrap the Page.captureScreenshot result as
    ``{"result": {"data": ...}}`` instead of the flat ``{"data": ...}`` shape.
    Handle both so a screenshot never silently degrades to empty bytes.
    """
    if isinstance(resp, dict):
        nested = resp.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("data"), str):
            return nested["data"]
        return resp.get("data", "")
    return ""


class Browser:
    def __init__(self, client: "Client", match: Match, *, human="natural") -> None:
        self._client = client
        self._match = match
        self._cdp_counter = 0
        self._pending_cdp: dict[int, asyncio.Future[Any]] = {}
        self._event_callbacks: list[EventCallback] = []
        self._tab_opened_callbacks: list[TabOpenedCallback] = []
        self._provider_disconnected_callbacks: list[SimpleCallback] = []
        self._provider_reconnected_callbacks: list[SimpleCallback] = []
        self._user_event_callbacks: list[UserEventCallback] = []
        self._ended = asyncio.Event()
        self._ended_reason: str | None = None

        from ._chat import BrowserChat
        from ._profile import BrowserProfile

        self.chat = BrowserChat(self)
        self.profile = BrowserProfile(self)

        env_profile = os.environ.get("CEKI_HUMAN_PROFILE")
        env_path = os.environ.get("CEKI_HUMAN_PROFILE_PATH")
        if human == "natural" and env_profile:
            human = env_profile
        elif human == "natural" and env_path:
            human = env_path
        self._humanizer = _resolve_human(human)
        self._last_pointer: tuple[int, int] | None = None
        self._last_seen_ts: str | None = None

        # P2P seamless fallback — once DC fails, stay on WS for this session
        self._p2p_fallback: bool = False

    @property
    def session_id(self) -> str:
        return self._match.session_id

    @property
    def browser_id(self) -> int:
        return self._match.schedule_id

    @property
    def schedule_id(self) -> int:
        return self._match.schedule_id

    @property
    def chat_topic_id(self) -> str | None:
        return self._match.chat_topic_id

    @property
    def browser_info(self) -> dict[str, Any]:
        return self._match.browser_info

    @property
    def provider_user_id(self) -> int | None:
        return self._match.provider_user_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def send(self, cdp: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        if self._ended.is_set():
            raise SessionEnded(self._ended_reason or "ended")
        cdp_id = self._cdp_counter
        self._cdp_counter += 1
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        p2p = self._client._p2p
        using_dc = p2p is not None
        # Tag the future so _on_cdp_response knows which transport
        # the response is expected on. When sent via DC, the WS relay
        # echoes a cdp_response that arrives first but has empty result
        # for large payloads (screenshot). Skip the WS echo and wait
        # for the DC response with full data.
        fut._cdp_transport = 'dc' if using_dc else 'ws'  # type: ignore[attr-defined]
        self._pending_cdp[cdp_id] = fut
        try:
            if using_dc and not self._p2p_fallback:
                # P2P path (preferred): wait for DC readiness then send CDP over DC.
                # The wait prevents a startup race where CDP goes over WS before the
                # DataChannel opens, congesting WS and starving the heartbeat ping.
                #
                # TimeoutError (DC still negotiating): WS for this one command, next
                # retries P2P — _p2p_fallback NOT set, so first CDP after DC opens
                # goes via P2P automatically.
                #
                # ConnectionError/OSError (DC broken): permanent WS fallback via
                # _p2p_fallback to avoid 30s wait on every subsequent command.
                try:
                    await asyncio.wait_for(p2p.wait_dc_open(), timeout=30.0)
                    await p2p.send_cdp({
                        "session_id": self.session_id,
                        "id": cdp_id,
                        "method": cdp["method"],
                        "params": cdp.get("params", {}),
                    })
                except asyncio.TimeoutError:
                    log.warning(
                        "cdp: P2P DC not ready within 30s for cmd %d — WS fallback for this cmd",
                        cdp_id,
                    )
                    fut._cdp_transport = 'ws'  # type: ignore[attr-defined]
                    log.debug("cdp: WS fallback sending cmd %d session=%s method=%s", cdp_id, self.session_id, cdp["method"])
                    await self._client._ws_send(
                        {
                            "type": "cdp",
                            "session_id": self.session_id,
                            "id": cdp_id,
                            "method": cdp["method"],
                            "params": cdp.get("params", {}),
                        }
                    )
                except (ConnectionError, OSError, Exception) as exc:
                    log.warning(
                        "cdp: P2P DC send failed for cmd %d: %s — fallback to WS",
                        cdp_id, exc,
                    )
                    self._p2p_fallback = True
                    fut._cdp_transport = 'ws'  # type: ignore[attr-defined]
                    await self._client._ws_send(
                        {
                            "type": "cdp",
                            "session_id": self.session_id,
                            "id": cdp_id,
                            "method": cdp["method"],
                            "params": cdp.get("params", {}),
                        }
                    )
            else:
                # WS path (fallback — used before P2P connects, when forced off,
                # or after a DC failure for this session)
                await self._client._ws_send(
                    {
                        "type": "cdp",
                        "session_id": self.session_id,
                        "id": cdp_id,
                        "method": cdp["method"],
                        "params": cdp.get("params", {}),
                    }
                )
            t0 = time.monotonic()
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            log.debug("cdp: cmd %d resolved in %.1fs", cdp_id, time.monotonic() - t0)
            return result
        finally:
            popped = self._pending_cdp.pop(cdp_id, None)
            if popped is not None and not popped.done():
                log.debug("cdp: cmd %d future still pending when popped from _pending_cdp!", cdp_id)

    def on_event(self, callback: EventCallback) -> None:
        self._event_callbacks.append(callback)

    def on_tab_opened(self, callback: TabOpenedCallback) -> None:
        self._tab_opened_callbacks.append(callback)

    def on_provider_disconnected(self, callback: SimpleCallback) -> None:
        self._provider_disconnected_callbacks.append(callback)

    def on_provider_reconnected(self, callback: SimpleCallback) -> None:
        self._provider_reconnected_callbacks.append(callback)

    def on_user_event(self, callback: UserEventCallback) -> None:
        self._user_event_callbacks.append(callback)

    async def switch_tab(self) -> None:
        await self._client._ws_send({"type": "switch_tab", "session_id": self.session_id})

    async def configure(self, *, masking_mode: str | None = None, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"type": "session.configure", "session_id": self.session_id}
        if masking_mode is not None:
            payload["masking_mode"] = masking_mode
        payload.update(kwargs)
        await self._client._ws_send(payload)

    async def close(self, *, timeout: float = 10.0) -> None:
        if self._ended.is_set():
            return
        try:
            await self._client._ws_send(
                {"type": "session.end", "session_id": self.session_id, "reason": "user_stop"}
            )
            await asyncio.wait_for(self._ended.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            for fut in self._pending_cdp.values():
                if not fut.done():
                    fut.cancel()
            self._pending_cdp.clear()
            self._ended.set()
            self._ended_reason = "user_stop"
        finally:
            self._client._active_browsers.pop(self.session_id, None)

    async def release(self, *, timeout: float = 10.0) -> None:
        """Alias for :meth:`close` — завершить аренду браузера."""
        await self.close(timeout=timeout)

    async def wait_until_ended(self) -> str:
        await self._ended.wait()
        return self._ended_reason or "unknown"

    # ──────────────────────────────────────────────────────────────────────────
    # High-level browser actions (with optional human-like timing)
    # ──────────────────────────────────────────────────────────────────────────

    def _humanize_for_call(self, human: bool | None) -> "Humanizer | None":
        # task 427 — per-call kill-switch. human=False bypasses humanizer
        # (timings) AND tells the extension to skip mouse-jitter via the
        # `_ceki_raw` param marker (see cdp.ts in ceki-browser-extension).
        # human=None → use session default (env / constructor). human=True
        # forces humanizer even if global env disabled it (corner case;
        # respects None humanizer if no profile).
        if human is False:
            return None
        return self._humanizer

    async def navigate(self, url: str, *, timeout: float = 30.0, human: bool | None = None) -> dict:
        h = self._humanize_for_call(human)
        if h:
            await h.before("navigate")
        result = await self.send(
            {"method": "Page.navigate", "params": {"url": url}}, timeout=timeout,
        )
        if h:
            await h.after("navigate")
        return result

    async def click(self, x: int | float, y: int | float, *, human: bool | None = None) -> None:
        h = self._humanize_for_call(human)
        if h:
            await h.before("click")
        raw_flag = {"_ceki_raw": True} if h is None else {}
        await self.send({"method": "Input.dispatchMouseEvent", "params": {
            "type": "mousePressed", "x": int(x), "y": int(y), "button": "left", "clickCount": 1,
            **raw_flag,
        }})
        await self.send({"method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseReleased", "x": int(x), "y": int(y), "button": "left", "clickCount": 1,
        }})
        self._last_pointer = (int(x), int(y))
        if h:
            await h.after("click")

    async def _send_keystroke(self, char: str) -> None:
        from .humanize.keymap import keymap_for_char
        mapping = keymap_for_char(char)
        if mapping is None:
            await self.send({"method": "Input.insertText", "params": {"text": char}})
            log.warning("Non-ASCII char %r: falling back to Input.insertText", char)
            return
        code, key, vk, needs_shift = mapping
        if needs_shift:
            await self.send({"method": "Input.dispatchKeyEvent", "params": {
                "type": "keyDown", "key": "Shift", "code": "ShiftLeft",
                "windowsVirtualKeyCode": 16, "nativeVirtualKeyCode": 16,
            }})
        await self.send({"method": "Input.dispatchKeyEvent", "params": {
            "type": "keyDown", "key": key, "code": code,
            "text": char, "unmodifiedText": char.lower() if needs_shift else char,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            **({"modifiers": 8} if needs_shift else {}),
        }})
        await self.send({"method": "Input.dispatchKeyEvent", "params": {
            "type": "keyUp", "key": key, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            **({"modifiers": 8} if needs_shift else {}),
        }})
        if needs_shift:
            await self.send({"method": "Input.dispatchKeyEvent", "params": {
                "type": "keyUp", "key": "Shift", "code": "ShiftLeft",
                "windowsVirtualKeyCode": 16, "nativeVirtualKeyCode": 16,
            }})

    async def type(
        self,
        text: str,
        *,
        selector: str | None = None,
        human: bool | None = None,
    ) -> None:
        # task 413 — typing humanizer moved into the extension. The SDK
        # now sends ONE Ceki.typeText command instead of N per-char
        # dispatchKeyEvent calls, so long inputs no longer burn through
        # the 500 cmd / 60s relay cap and inter-key delays land without
        # WS jitter. The extension owns keymap + profile timings.
        #
        # task 425 BUG-1 — optional `selector` is forwarded to the extension
        # which focuses the matching element via chrome.scripting.executeScript
        # across all frames. The previous SDK-side Runtime.evaluate hit
        # "ReferenceError: document is not defined" on signup.live.com et al.
        # because Chrome routed the bare CDP eval to the page's service-worker
        # execution context where `document` is undefined. chrome.scripting
        # always lands in a page frame.
        #
        # task 4109 — anti-detect branching. For LONG text delivered into a
        # KNOWN selector, roll the dice against TYPE_PASTE_PROBABILITY and (if
        # the gate opens) route through the real-clipboard Ctrl+V path from
        # task 4098 instead of Ceki.typeText. Reasons this branch is gated on
        # both `selector` and length:
        #   - no selector => we don't know where to focus for the OS paste,
        #     so the per-char path (which types into current focus) is the
        #     only sane fallback;
        #   - short text has no rhythm-signature problem to begin with, and
        #     paste-events on short strings look weirder than per-key ones.
        # Humanizer pre-click / after-hooks still run — the selector focus
        # inside _hotkey_paste_into replaces the extension's focus step for
        # this branch.
        if (
            selector is not None
            and len(text) > TYPE_PASTE_MIN_CHARS
            and random.random() < TYPE_PASTE_PROBABILITY
        ):
            h_pre = self._humanize_for_call(human)
            if h_pre:
                await h_pre.before("type")
            await self._hotkey_paste_into(selector, text)
            if h_pre:
                await h_pre.after("type")
            return

        h = self._humanize_for_call(human)
        if h:
            if self._last_pointer is not None and selector is None:
                await self.click(*self._last_pointer)
            elif selector is None:
                log.debug(
                    "type() called with humanizer but no last_pointer;"
                    " input may not land on the intended element"
                )
            await h.before("type")

        human_name: str | None = None
        if h and h.profile:
            name = h.profile.name
            human_name = name if name in ("natural", "careful") else "natural"

        params: dict[str, Any] = {"text": text, "human": human_name}
        if selector is not None:
            params["selector"] = selector

        await self.send({"method": "Ceki.typeText", "params": params})

        if h:
            await h.after("type")

    async def scroll(
        self, x: int = 0, y: int = 0, *, delta_x: int = 0, delta_y: int = -300,
        human: bool | None = None,
    ) -> None:
        h = self._humanize_for_call(human)
        if h:
            await h.before("scroll")
        await self.send({"method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y,
        }})
        self._last_pointer = (int(x), int(y))
        if h:
            await h.after("scroll")

    async def screenshot(
        self,
        *,
        format: Literal["base64", "png"] = "base64",
        full_page: bool = False,
        timeout: float = 120.0,
    ) -> dict | bytes:
        """Take a screenshot.

        Args:
            format: ``"base64"`` (default) returns CDP-shape dict, ``"png"`` returns raw PNG bytes.
            full_page: If True, capture the entire scrollable page, not just the viewport.
            timeout: CDP timeout in seconds (default 120 — heavy pages like
                signup.live.com routinely take 60+ seconds to capture, task 425).
        """
        if format not in ("base64", "png"):
            raise ValueError(f"Unsupported format: {format!r}. Use 'base64' or 'png'.")
        if self._humanizer:
            await self._humanizer.before("screenshot")

        # task 425 BUG-3 — `optimizeForSpeed: true` skips the JPEG quality
        # tuning Chrome would otherwise run when the page is still painting
        # (signup.live.com lazy-loads frames for ~minutes). Combined with
        # the bumped timeout this turns 60s timeouts into sub-second captures.
        params: dict[str, Any] = {"optimizeForSpeed": True}
        if full_page:
            metrics = await self.send({"method": "Page.getLayoutMetrics"}, timeout=timeout)
            content = metrics.get("contentSize", {})
            width = int(content.get("width", 0))
            height = int(content.get("height", 0))
            MAX_HEIGHT = 16384
            if height > MAX_HEIGHT:
                log.warning("full_page screenshot height=%d clamped to %d", height, MAX_HEIGHT)
                height = MAX_HEIGHT
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}

        resp = await self.send(
            {"method": "Page.captureScreenshot", "params": params}, timeout=timeout,
        )
        if self._humanizer:
            await self._humanizer.after("screenshot")
        if format == "base64":
            return {"data": _unwrap_screenshot_data(resp)}
        import base64 as _b64
        data = _unwrap_screenshot_data(resp)
        return _b64.b64decode(data) if data else b""

    async def snapshot(self, *, timeout: float = 120.0) -> Snapshot:
        from datetime import datetime, timezone
        # task 425 BUG-3 — same `optimizeForSpeed` + bumped timeout as
        # screenshot(); heavy pages would otherwise hit the 60s default.
        resp = await self.send(
            {"method": "Page.captureScreenshot", "params": {"optimizeForSpeed": True}},
            timeout=timeout,
        )
        screenshot_b64 = _unwrap_screenshot_data(resp)
        all_msgs = await self.chat.history(since=self._last_seen_ts)
        if self._last_seen_ts and all_msgs:
            all_msgs = [m for m in all_msgs if m.created_at > self._last_seen_ts]
        if all_msgs:
            self._last_seen_ts = all_msgs[-1].created_at
        return Snapshot(screenshot=screenshot_b64, chat=all_msgs, ts=datetime.now(timezone.utc))

    @staticmethod
    def _detect_mime(filename: str) -> str:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or "application/octet-stream"

    async def upload(
        self,
        selector: str,
        source: str | Path | bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict:
        """Upload a file to an ``<input type="file">`` element.

        Args:
            selector: CSS selector for the file input element.
            source: File path (str/Path) or raw bytes.
            filename: Override the filename (default: basename of path or ``upload.bin``).
            mime_type: Override MIME type (default: auto-detect from extension).

        Returns:
            ``{"ok": True, "filename": "...", "size": N}`` on success.

        Raises:
            ValueError: If selector matches no element or element is not a file input.
        """
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.is_file():
                raise ValueError(f"file not found: {path}")
            data = path.read_bytes()
            if filename is None:
                filename = path.name
        elif isinstance(source, bytes):
            data = source
            if filename is None:
                filename = "upload.bin"
        else:
            raise TypeError(f"source must be str, Path, or bytes, got {type(source).__name__}")

        if mime_type is None:
            mime_type = self._detect_mime(filename)

        log.info("upload: file=%s mime=%s size=%d", filename, mime_type, len(data))

        b64_data = base64.b64encode(data).decode("ascii")

        js_selector = json.dumps(selector)
        js_filename = json.dumps(filename)
        js_mimetype = json.dumps(mime_type)

        js_expr = (
            "(function() {"
            f"var input = document.querySelector({js_selector});"
            "if (!input) return JSON.stringify({error: 'no input matched'});"
            "if (input.type !== 'file')"
            " return JSON.stringify({error: 'element is not a file input'});"
            f"var b64 = '{b64_data}';"
            "var bin = atob(b64);"
            "var bytes = new Uint8Array(bin.length);"
            "for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
            f"var file = new File([bytes], {js_filename}, {{type: {js_mimetype}}});"
            "var dt = new DataTransfer();"
            "dt.items.add(file);"
            "input.files = dt.files;"
            "input.dispatchEvent(new Event('change', {bubbles: true}));"
            f"return JSON.stringify({{ok: true, filename: {js_filename}, size: bytes.length}});"
            "})()"
        )

        resp = await self.send(
            {"method": "Runtime.evaluate", "params": {"expression": js_expr, "returnByValue": True}}
        )

        value = resp.get("result", {}).get("value", "")
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value

        if "error" in parsed:
            raise ValueError(parsed["error"])

        try:
            esc_params = {
                "key": "Escape",
                "code": "Escape",
                "windowsVirtualKeyCode": 27,
                "nativeVirtualKeyCode": 27,
            }
            await self.send({
                "method": "Input.dispatchKeyEvent",
                "params": {"type": "keyDown", **esc_params},
            })
            await self.send({
                "method": "Input.dispatchKeyEvent",
                "params": {"type": "keyUp", **esc_params},
            })
        except Exception:
            pass

        return parsed

    async def _dispatch_hotkey(self, key: str, code: str) -> None:
        """Dispatch a Ctrl+<key> hotkey as ``keyDown``+``keyUp`` via CDP.

        ``modifiers=2`` is Chromium's bitmask for Control. We fire both
        ``keyDown`` and ``keyUp`` because the browser's clipboard shortcuts
        only trigger on a full press cycle. Used by :meth:`copy` (Ctrl+C)
        and :meth:`paste` (Ctrl+C on the seed textarea, then Ctrl+V on the
        target).
        """
        vk = ord(key.upper())
        params = {
            "modifiers": 2,
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        }
        await self.send({
            "method": "Input.dispatchKeyEvent",
            "params": {"type": "keyDown", **params},
        })
        await self.send({
            "method": "Input.dispatchKeyEvent",
            "params": {"type": "keyUp", **params},
        })

    async def copy(self) -> str:
        """Copy the current window selection into the OS clipboard, return it.

        Reads the current selection text via ``Runtime.evaluate``
        (``window.getSelection().toString()``) so the caller still gets it as a
        return value, then dispatches a synthetic ``Ctrl+C`` via
        ``Input.dispatchKeyEvent`` — that is the step that actually flips the OS
        clipboard. Verified against real headed Chromium in contract task 4098.

        The reason we read the selection before Ctrl+C rather than reading it
        back from the clipboard: the main-mode CDP allowlist forbids
        ``navigator.clipboard``, and ``document.execCommand('paste')`` is dead
        in modern Chromium, so there's no read-back path from JS. Reading the
        selection directly is cheap and gives an exact return value.

        Returns:
            The selection text (``""`` when nothing is selected). The OS
            clipboard is flipped as a side effect regardless of the return.
        """
        result = await self.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": "window.getSelection().toString()",
                "returnByValue": True,
            },
        })
        selection = (result.get("result") or {}).get("value") or ""
        await self._dispatch_hotkey("c", "KeyC")
        return selection

    async def _hotkey_paste_into(self, selector: str, text: str) -> None:
        """Real system-clipboard paste of ``text`` into ``selector``.

        Shared 6-CDP-call sequence (from task 4098):

          1. Runtime.evaluate — build offscreen ``<textarea>``, set value, focus+select
          2. Input.dispatchKeyEvent keyDown ``c`` (Ctrl+C — flips OS clipboard)
          3. Input.dispatchKeyEvent keyUp   ``c``
          4. Runtime.evaluate — remove temp element, focus target selector
          5. Input.dispatchKeyEvent keyDown ``v`` (Ctrl+V — fires paste event)
          6. Input.dispatchKeyEvent keyUp   ``v``

        Both ``selector`` and ``text`` are JSON-escaped when interpolated —
        quotes, backticks, backslashes, newlines, and unicode are safe.

        Called by :meth:`paste` (public API) and :meth:`type` (task 4109
        anti-detect branch for long text). Extracting this keeps the two
        callers wire-identical and avoids the copy() logging noise inside a
        type() call.
        """
        text_lit = json.dumps(text)
        seed_expr = (
            "(function(){"
            "var __ceki_tmp__=document.createElement('textarea');"
            "__ceki_tmp__.id='__ceki_paste_tmp__';"
            "__ceki_tmp__.style.cssText='position:fixed;left:-9999px;top:0;opacity:0';"
            f"__ceki_tmp__.value={text_lit};"
            "document.body.appendChild(__ceki_tmp__);"
            "__ceki_tmp__.focus();__ceki_tmp__.select();"
            "})()"
        )
        await self.send({
            "method": "Runtime.evaluate",
            "params": {"expression": seed_expr},
        })
        await self._dispatch_hotkey("c", "KeyC")

        selector_lit = json.dumps(selector)
        cleanup_focus_expr = (
            "(function(){"
            "var t=document.getElementById('__ceki_paste_tmp__');"
            "if(t)t.remove();"
            f"var el=document.querySelector({selector_lit});"
            "el.focus();"
            "})()"
        )
        await self.send({
            "method": "Runtime.evaluate",
            "params": {"expression": cleanup_focus_expr},
        })
        await self._dispatch_hotkey("v", "KeyV")

    async def paste(self, selector: str, text: str) -> None:
        """Put ``text`` into the OS clipboard, focus ``selector``, Ctrl+V it in.

        Real system-clipboard paste: a temporary offscreen ``<textarea>`` is
        created and selected, then a synthetic ``Ctrl+C`` flips the OS
        clipboard to ``text``. The temp element is removed, the target element
        is focused, and a synthetic ``Ctrl+V`` fires — which dispatches a real
        ``ClipboardEvent`` (``paste`` handler + ``input`` event with
        ``inputType='insertFromPaste'``). Verified against real headed
        Chromium in contract task 4098.

        Both ``selector`` and ``text`` are JSON-escaped when interpolated into
        the ``Runtime.evaluate`` expression — quotes, backticks, backslashes,
        newlines, and unicode are safe.

        Args:
            selector: CSS selector for the target input / textarea /
                contentEditable / any focusable element.
            text: Arbitrary string to paste. Empty string is allowed; it
                seeds an empty clipboard and Ctrl+V still fires the
                ``paste`` event.

        Raises:
            The underlying ``Runtime.evaluate`` will surface a JS TypeError if
            ``querySelector`` returns ``null`` — the send() call will reject
            with a CDP error rather than silently swallowing it.
        """
        await self._hotkey_paste_into(selector, text)

    def set_human(self, profile) -> "HumanProfile | None":
        prev = self._humanizer.profile if self._humanizer else None
        self._humanizer = _resolve_human(profile)
        return prev

    # ──────────────────────────────────────────────────────────────────────────
    # Human action / captcha
    # ──────────────────────────────────────────────────────────────────────────

    def _api_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Authorization": f"Bearer {self._client.api_key}"}
        if self._client._basic_auth:
            creds = base64.b64encode(
                f"{self._client._basic_auth[0]}:{self._client._basic_auth[1]}".encode()
            ).decode()
            headers["X-Basic-Auth"] = f"Basic {creds}"
        return headers

    async def request_captcha(
        self,
        acceptance_timeout: float = 60,
        completion_timeout: float = 120,
        auto_accept: bool = True,
    ) -> CaptchaResult:
        if acceptance_timeout < 30:
            raise ValueError("acceptance_timeout must be >= 30 seconds")
        if completion_timeout < 30:
            raise ValueError("completion_timeout must be >= 30 seconds")

        acceptance_timeout = min(acceptance_timeout, 300)
        completion_timeout = min(completion_timeout, 600)

        child_event_id = await self._create_captcha_event(acceptance_timeout, completion_timeout)

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.chat._action_queues[child_event_id] = queue

        accepted = False
        completion_deadline = datetime.now(timezone.utc) + timedelta(seconds=completion_timeout)

        try:
            deadline_accept = asyncio.get_event_loop().time() + acceptance_timeout
            while True:
                remaining = deadline_accept - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                action = await asyncio.wait_for(queue.get(), timeout=remaining)
                kind = action.get("kind", "")
                data: dict[str, Any] = action.get("data") or {}

                if kind == "human_action_accepted":
                    accepted = True
                    break
                if kind == "human_action_completed":
                    return await self._finish_captcha(
                        child_event_id, data, auto_accept, solved=True,
                    )
                if kind in (
                    "human_action_failed",
                    "human_action_declined",
                    "human_action_withdrew",
                ):
                    return CaptchaResult(
                        solved=False,
                        child_event_id=child_event_id,
                        cancel_reason=kind.replace("human_action_", ""),
                        browser=self,
                    )

            remaining_completion = (
                completion_deadline - datetime.now(timezone.utc)
            ).total_seconds()
            while True:
                if remaining_completion <= 0:
                    raise asyncio.TimeoutError()
                action = await asyncio.wait_for(
                    queue.get(), timeout=remaining_completion,
                )
                kind = action.get("kind", "")
                data = action.get("data") or {}

                if kind == "human_action_completed":
                    return await self._finish_captcha(
                        child_event_id, data, auto_accept, solved=True,
                    )
                if kind in ("human_action_failed", "human_action_withdrew"):
                    return CaptchaResult(
                        solved=False,
                        child_event_id=child_event_id,
                        cancel_reason=kind.replace("human_action_", ""),
                        browser=self,
                    )
                remaining_completion = (
                    completion_deadline - datetime.now(timezone.utc)
                ).total_seconds()

        except asyncio.TimeoutError:
            phase = "completion" if accepted else "acceptance"
            await self._expire_captcha_event(child_event_id)
            raise CaptchaTimeoutError(phase) from None
        finally:
            self.chat._action_queues.pop(child_event_id, None)

    async def _finish_captcha(
        self,
        child_event_id: int,
        data: dict[str, Any],
        auto_accept: bool,
        *,
        solved: bool,
    ) -> CaptchaResult:
        result = CaptchaResult(
            solved=solved,
            child_event_id=child_event_id,
            proof_message_id=data.get("proof_message_id"),
            correction_id=data.get("correction_id"),
            browser=self,
        )
        if auto_accept and solved and result.correction_id:
            await asyncio.sleep(2)
            await result.accept_work()
        return result

    async def _create_captcha_event(
        self, acceptance_timeout: float, completion_timeout: float,
    ) -> int:
        body = {
            "acceptance_deadline_at": int(acceptance_timeout),
            "completion_deadline_at": int(completion_timeout),
        }
        url = f"{self._client.api_url}/api/agent/sessions/{self._match.event_id}/captcha-request"
        backoff = [0.5, 1.0, 2.0, 4.0]
        last_resp = None
        for delay in [0.0] + backoff:
            if delay:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    url,
                    headers={**self._api_headers(), "Content-Type": "application/json"},
                    json=body,
                )
            last_resp = resp
            if resp.status_code != 422 or "Session not active" not in (resp.text or ""):
                break
        last_resp.raise_for_status()
        result = last_resp.json()
        event_id = result.get("id")
        if not event_id:
            raise RuntimeError("captcha request did not return an id")
        return int(event_id)

    async def _expire_captcha_event(self, child_event_id: int) -> None:
        try:
            async with httpx.AsyncClient() as http:
                await http.patch(
                    f"{self._client.api_url}/api/agent/kal/event/{child_event_id}",
                    headers={**self._api_headers(), "Content-Type": "application/json"},
                    json={"status_id": 777},
                )
        except Exception as exc:
            log.warning("expire captcha event %d failed: %s", child_event_id, exc)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal dispatch (called from Client._reader_loop)
    # ──────────────────────────────────────────────────────────────────────────

    async def _on_cdp_response(self, msg: dict[str, Any]) -> None:
        cmd_id = msg.get("id")
        log.debug("_on_cdp_response: id=%s pending_keys=%s", cmd_id, list(self._pending_cdp.keys()))
        if cmd_id is not None and cmd_id in self._pending_cdp:
            fut = self._pending_cdp[cmd_id]
            if not fut.done():
                # When a command was sent via DC (ceki-cmd data channel),
                # the relay also echoes a WS cdp_response that races ahead
                # but has empty result for large payloads (screenshot).
                # Skip the WS echo and wait for the DC response.
                transport = getattr(fut, '_cdp_transport', 'ws')
                is_from_ws = msg.get("type") == "cdp_response" or "session_id" in msg
                log.debug("_on_cdp_response: transport=%s is_from_ws=%s skip=%s", transport, is_from_ws, transport == 'dc' and is_from_ws)
                if transport == 'dc' and is_from_ws:
                    log.debug("cdp: skip WS echo for DC-sent command id=%s", cmd_id)
                    return
                self._pending_cdp.pop(cmd_id)
                if msg.get("ok", True):
                    log.debug("_on_cdp_response: resolving future with OK")
                    fut.set_result(msg.get("result", {}))
                else:
                    err = msg.get("error", {})
                    log.debug("_on_cdp_response: resolving future with error %s", err)
                    fut.set_exception(Exception(f"CDP error {err}"))
        else:
            log.debug("_on_cdp_response: id=%s NOT in pending (keys=%s) or None", cmd_id, list(self._pending_cdp.keys()))

    async def _on_cdp_event(self, msg: dict[str, Any]) -> None:
        method = msg.get("method", "")
        params = msg.get("params", {})
        for cb in self._event_callbacks:
            asyncio.create_task(cast(Coroutine, cb(method, params)))

    async def _on_tab_opened(self, msg: dict[str, Any]) -> None:
        url = msg.get("url", "")
        for cb in self._tab_opened_callbacks:
            asyncio.create_task(cast(Coroutine, cb(url)))

    async def _on_session_ended(self, msg: dict[str, Any]) -> None:
        reason = msg.get("reason", "completed")
        self._ended_reason = reason
        if reason == "provider_disconnected":
            exc: Exception = ProviderDisconnected()
        else:
            exc = SessionEnded(reason)
        for fut in self._pending_cdp.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending_cdp.clear()
        self._ended.set()
        self._client._active_browsers.pop(self.session_id, None)

    async def _on_provider_disconnected(self, msg: dict[str, Any]) -> None:
        for cb in self._provider_disconnected_callbacks:
            asyncio.create_task(cast(Coroutine, cb()))

    async def _on_provider_reconnected(self, msg: dict[str, Any]) -> None:
        for cb in self._provider_reconnected_callbacks:
            asyncio.create_task(cast(Coroutine, cb()))

    async def _on_user_events(self, msg: dict[str, Any]) -> None:
        events: list[dict[str, Any]] = msg.get("events", [])
        for cb in self._user_event_callbacks:
            asyncio.create_task(cast(Coroutine, cb(events)))

    async def _on_error(self, msg: dict[str, Any]) -> None:
        code = msg.get("code", 0)
        cmd_id = msg.get("id")

        if code == -1013:
            exc: Exception = RateLimitExceeded(retry_after=float(msg.get("retry_after", 1.0)))
            if cmd_id is not None and cmd_id in self._pending_cdp:
                fut = self._pending_cdp.pop(cmd_id)
                if not fut.done():
                    fut.set_exception(exc)
            return

        if code == -1050:
            last_err = msg.get("last_error", msg.get("message", "cdp_error"))
            exc = CdpUnrecoverable(last_error=str(last_err))
            if cmd_id is not None and cmd_id in self._pending_cdp:
                fut = self._pending_cdp.pop(cmd_id)
                if not fut.done():
                    fut.set_exception(exc)
            return

        if code == -1011:
            reason = "heartbeat_timeout"
        elif code == -1012:
            reason = "insufficient_funds"
        elif code == -1015:
            reason = "provider_declined"
        elif code == -1018:
            reason = "killed"
        else:
            reason = msg.get("reason") or msg.get("message") or f"error_{code}"

        self._ended_reason = reason
        terminal_exc: Exception
        if code == -1012:
            terminal_exc = InsufficientFunds()
        else:
            terminal_exc = SessionEnded(reason)

        for fut in self._pending_cdp.values():
            if not fut.done():
                fut.set_exception(terminal_exc)
        self._pending_cdp.clear()
        self._ended.set()
        self._client._active_browsers.pop(self.session_id, None)

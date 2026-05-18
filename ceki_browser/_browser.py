from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, Literal, cast

import httpx

from .humanize import HumanProfile, Humanizer

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

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
TabOpenedCallback = Callable[[str], Awaitable[None]]
SimpleCallback = Callable[[], Awaitable[None]]
UserEventCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]

_ERROR_TERMINAL = {-1011, -1012, -1015, -1018}


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

    async def send(self, cdp: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        if self._ended.is_set():
            raise SessionEnded(self._ended_reason or "ended")
        cdp_id = self._cdp_counter
        self._cdp_counter += 1
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending_cdp[cdp_id] = fut
        try:
            await self._client._ws_send(
                {
                    "type": "cdp",
                    "session_id": self.session_id,
                    "id": cdp_id,
                    "method": cdp["method"],
                    "params": cdp.get("params", {}),
                }
            )
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            return result
        finally:
            self._pending_cdp.pop(cdp_id, None)

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

    async def navigate(self, url: str, *, timeout: float = 30.0) -> dict:
        if self._humanizer:
            await self._humanizer.before("navigate")
        result = await self.send({"method": "Page.navigate", "params": {"url": url}}, timeout=timeout)
        if self._humanizer:
            await self._humanizer.after("navigate")
        return result

    async def click(self, x: int | float, y: int | float) -> None:
        if self._humanizer:
            await self._humanizer.before("click")
        await self.send({"method": "Input.dispatchMouseEvent", "params": {
            "type": "mousePressed", "x": int(x), "y": int(y), "button": "left", "clickCount": 1,
        }})
        await self.send({"method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseReleased", "x": int(x), "y": int(y), "button": "left", "clickCount": 1,
        }})
        self._last_pointer = (int(x), int(y))
        if self._humanizer:
            await self._humanizer.after("click")

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

    async def type(self, text: str) -> None:
        if self._humanizer:
            if self._last_pointer is not None:
                await self.click(*self._last_pointer)
            else:
                log.debug("type() called with humanizer but no last_pointer; falling back to plain insertText")
            await self._humanizer.before("type")
            async for char, delay_ms in self._humanizer.humanize_text(text):
                await self._send_keystroke(char)
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
            await self._humanizer.after("type")
        else:
            for char in text:
                await self._send_keystroke(char)

    async def scroll(
        self, x: int = 0, y: int = 0, *, delta_x: int = 0, delta_y: int = -300
    ) -> None:
        if self._humanizer:
            await self._humanizer.before("scroll")
        await self.send({"method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y,
        }})
        self._last_pointer = (int(x), int(y))
        if self._humanizer:
            await self._humanizer.after("scroll")

    async def screenshot(
        self,
        *,
        format: Literal["base64", "png"] = "base64",
        full_page: bool = False,
    ) -> dict | bytes:
        """Take a screenshot.

        Args:
            format: ``"base64"`` (default) returns CDP-shape dict, ``"png"`` returns raw PNG bytes.
            full_page: If True, capture the entire scrollable page, not just the viewport.
        """
        if format not in ("base64", "png"):
            raise ValueError(f"Unsupported format: {format!r}. Use 'base64' or 'png'.")
        if self._humanizer:
            await self._humanizer.before("screenshot")

        params: dict[str, Any] = {}
        if full_page:
            metrics = await self.send({"method": "Page.getLayoutMetrics"})
            content = metrics.get("contentSize", {})
            width = int(content.get("width", 0))
            height = int(content.get("height", 0))
            MAX_HEIGHT = 16384
            if height > MAX_HEIGHT:
                log.warning("full_page screenshot height=%d clamped to %d", height, MAX_HEIGHT)
                height = MAX_HEIGHT
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}

        resp = await self.send({"method": "Page.captureScreenshot", "params": params})
        if self._humanizer:
            await self._humanizer.after("screenshot")
        if format == "base64":
            return resp
        import base64 as _b64
        data = resp.get("data", "")
        return _b64.b64decode(data) if data else b""

    async def snapshot(self) -> Snapshot:
        from datetime import datetime, timezone
        resp = await self.send({"method": "Page.captureScreenshot"})
        screenshot_b64 = resp.get("data", "")
        all_msgs = await self.chat.history(since=self._last_seen_ts)
        if self._last_seen_ts and all_msgs:
            all_msgs = [m for m in all_msgs if m.created_at > self._last_seen_ts]
        if all_msgs:
            self._last_seen_ts = all_msgs[-1].created_at
        return Snapshot(screenshot=screenshot_b64, chat=all_msgs, ts=datetime.now(timezone.utc))

    async def upload(
        self,
        selector: str,
        source: str | Path | bytes,
        *,
        filename: str | None = None,
    ) -> dict:
        """Upload a file to an ``<input type="file">`` element.

        Args:
            selector: CSS selector for the file input element.
            source: File path (str/Path) or raw bytes.
            filename: Override the filename (default: basename of path or ``upload.bin``).

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

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"

        b64_data = base64.b64encode(data).decode("ascii")

        js_selector = json.dumps(selector)
        js_filename = json.dumps(filename)
        js_mimetype = json.dumps(mime_type)

        js_expr = (
            "(function() {"
            f"var input = document.querySelector({js_selector});"
            "if (!input) return JSON.stringify({error: 'no input matched'});"
            "if (input.type !== 'file') return JSON.stringify({error: 'element is not a file input'});"
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

        return parsed

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
            "parent_id": int(self._match.event_id) if self._match.event_id else None,
            "kal_schedule_id": self._match.schedule_id,
            "billable_type": "App\\Models\\Agent",
            "benefitable_type": "App\\Models\\User",
            "benefitable_id": self._match.provider_user_id,
            "amount": 0.10,
            "status_id": 100,
            "data": {
                "action_type": "captcha",
                "acceptance_deadline_at": int(acceptance_timeout),
                "completion_deadline_at": int(completion_timeout),
            },
        }
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{self._client.api_url}/api/agent/kal/event/store",
                headers={**self._api_headers(), "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
        result = resp.json()
        event_id = result.get("id") or (result.get("data") or {}).get("id")
        if not event_id:
            raise RuntimeError("event creation did not return an id")
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
        if cmd_id is not None and cmd_id in self._pending_cdp:
            fut = self._pending_cdp.pop(cmd_id)
            if not fut.done():
                if msg.get("ok", True):
                    fut.set_result(msg.get("result", {}))
                else:
                    err = msg.get("error", {})
                    fut.set_exception(Exception(f"CDP error {err}"))

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

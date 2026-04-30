from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

from .chat_direct import ChatClient, DEFAULT_CHAT_SERVICE_URL
from .errors import CekiBrowserError, NoMatchError, SessionEndedError
from .humanize import HumanProfile, Humanizer
from .transport import Transport
from .transport_rtc import ChatImage, ChatTextMessage, RTCTransport
from .types import (
    HtmlResult,
    HumanActionResult,
    NavigateResult,
    QueryResult,
    ScreenshotResult,
    parse_result,
)

logger = logging.getLogger("ceki_browser")


def _resolve_human_profile(human: Any) -> HumanProfile | None:
    """Resolve human parameter to HumanProfile or None."""
    if os.environ.get("CEKI_HUMAN_DISABLE") == "1":
        return None
    if human is None:
        return None
    if isinstance(human, HumanProfile):
        return human
    if isinstance(human, dict):
        return HumanProfile.from_dict(human)
    if isinstance(human, Path):
        return HumanProfile.load(human)
    if isinstance(human, str):
        # Check if it's a file path
        if human.endswith(".json") or "/" in human or "\\" in human:
            return HumanProfile.load(human)
        # It's a preset name
        return HumanProfile.load_preset(human)
    raise ValueError(f"Invalid human profile: {human!r}")


_HUMAN_DEFAULT = object()  # sentinel for "use default"


def _get_default_human() -> Any:
    """Get default human profile from env or 'natural'."""
    if os.environ.get("CEKI_HUMAN_DISABLE") == "1":
        return None
    env_path = os.environ.get("CEKI_HUMAN_PROFILE_PATH")
    if env_path:
        return env_path
    env_name = os.environ.get("CEKI_HUMAN_PROFILE")
    if env_name:
        return env_name
    return "natural"


class ChatAPI:
    def __init__(self, rtc: RTCTransport):
        self._rtc = rtc

    @property
    def available(self) -> bool:
        return self._rtc.chat_channel is not None and self._rtc.chat_channel.readyState == "open"

    async def send(self, text: str) -> None:
        await self._rtc.send_chat_text(text)

    async def send_image(
        self,
        data: bytes | str,
        mime: str | None = None,
    ) -> None:
        await self._rtc.send_chat_image(data, mime)

    def on_message(self, callback: Callable[[ChatTextMessage], Any]) -> None:
        self._rtc.on_chat_message(callback)

    def on_image(self, callback: Callable[[ChatImage], Any]) -> None:
        self._rtc.on_chat_image(callback)

    @property
    def history(self) -> list[ChatTextMessage | ChatImage]:
        return self._rtc.chat_history


class Session:
    def __init__(
        self,
        transport: Transport,
        request_id: str,
        mode: str,
        ice_servers: list[dict[str, Any]] | None = None,
        human: Any = _HUMAN_DEFAULT,
    ):
        self._transport = transport
        self._request_id = request_id
        self._session_id: str | None = None
        self._mode = mode
        self._active = False
        self._rtc: RTCTransport | None = None
        self._chat: ChatAPI | None = None
        self._ice_servers = ice_servers or [{"urls": "stun:stun.l.google.com:19302"}]
        self._tab_opened_callback: Callable[[dict[str, Any]], Any] | None = None
        self._chat_direct: ChatClient | None = None
        if human is _HUMAN_DEFAULT:
            human = _get_default_human()
        self._human_profile = _resolve_human_profile(human)
        self._humanizer = Humanizer(self._human_profile)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def active(self) -> bool:
        return self._active

    @property
    def chat(self) -> ChatAPI:
        if self._chat is None:
            raise CekiBrowserError("Chat not available until session is active with P2P connection")
        return self._chat

    @property
    def rtc(self) -> RTCTransport | None:
        return self._rtc

    @property
    def humanizer(self) -> Humanizer:
        return self._humanizer

    def set_human(self, profile: Any) -> HumanProfile | None:
        prev = self._human_profile
        self._human_profile = _resolve_human_profile(profile)
        self._humanizer = Humanizer(self._human_profile)
        return prev

    def _install_match_listener(self) -> tuple[asyncio.Event, list[str], list[Exception]]:
        ready = asyncio.Event()
        session_id_holder: list[str] = []
        error_holder: list[Exception] = []

        original_cb = self._transport._event_callback
        self._original_cb_for_match = original_cb

        async def _on_event(method: str, params: dict[str, Any]) -> None:
            if method == "session.matched":
                sid = params.get("session_id", "")
                session_id_holder.append(sid)
                ready.set()
            elif method == "session.no_match":
                reason = params.get("reason", "No matching providers available")
                error_holder.append(NoMatchError(reason))
                ready.set()
            elif method == "session.ended":
                reason = params.get("reason", "ended_before_active")
                error_holder.append(SessionEndedError(reason))
                ready.set()
            if original_cb:
                result = original_cb(method, params)
                if asyncio.iscoroutine(result):
                    await result

        self._transport.on_event(_on_event)
        return ready, session_id_holder, error_holder

    async def _wait_for_active(self, timeout: float = 60.0) -> None:
        ready, session_id_holder, error_holder = self._match_state
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise CekiBrowserError("Timed out waiting for session to become active")
        finally:
            self._transport.on_event(self._original_cb_for_match)

        if error_holder:
            raise error_holder[0]

        if session_id_holder:
            self._session_id = session_id_holder[0]
        self._active = True

        await self._setup_rtc()

    async def _setup_rtc(self) -> None:
        self._rtc = RTCTransport(self._ice_servers)
        self._chat = ChatAPI(self._rtc)

        signaling_done = asyncio.Event()
        answer_holder: list[dict[str, Any]] = []

        original_cb = self._transport._event_callback

        async def _on_signaling(method: str, params: dict[str, Any]) -> None:
            if method == "webrtc.answer":
                answer_holder.append(params)
                signaling_done.set()
            elif method == "webrtc.ice":
                await self._rtc.add_ice(params)
            elif method == "session.ended":
                self._active = False
                signaling_done.set()
            if original_cb:
                result = original_cb(method, params)
                if asyncio.iscoroutine(result):
                    await result

        self._rtc.on_signaling(lambda method, params: asyncio.ensure_future(
            self._transport.notify(method, {
                "session_id": self._session_id,
                **(params or {}),
            })
        ))

        self._transport.on_event(_on_signaling)

        offer = await self._rtc.create_offer()
        await self._transport.notify("webrtc.offer", {
            "session_id": self._session_id,
            "sdp": offer["sdp"],
            "type": offer["type"],
        })

        try:
            await asyncio.wait_for(signaling_done.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise CekiBrowserError("Timed out waiting for WebRTC answer")

        if not answer_holder:
            raise CekiBrowserError("Session ended before RTC handshake completed")

        await self._rtc.apply_answer(answer_holder[0])
        await self._rtc.wait_connected(timeout=15.0)

        self._install_session_event_handler()
        logger.info("P2P connection established for session %s", self._session_id)

    def _install_session_event_handler(self) -> None:
        original_cb = self._transport._event_callback

        async def _on_event(method: str, params: dict[str, Any]) -> None:
            if method == "session.ended":
                self._active = False
            elif method == "tab.opened":
                if self._tab_opened_callback:
                    result = self._tab_opened_callback(params)
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    tab_id = params.get("tab_id")
                    if tab_id is not None:
                        try:
                            await self._rtc.send_command("tabs.close", {"session_id": self._session_id, "tab_id": tab_id})
                        except Exception:
                            pass
            if original_cb:
                result = original_cb(method, params)
                if asyncio.iscoroutine(result):
                    await result

        self._transport.on_event(_on_event)

    async def navigate(self, url: str, timeout_ms: int = 120000) -> NavigateResult:
        self._check_active()
        await self._humanizer.before("navigate")
        data = await self._rtc.send_command(
            "browser.navigate",
            {"url": url, "timeout_ms": timeout_ms},
            timeout=timeout_ms / 1000 + 5,
        )
        await self._humanizer.after("navigate")
        return parse_result(data, NavigateResult)

    async def query(self, selector: str, attributes: list[str] | None = None) -> QueryResult:
        self._check_active()
        params: dict[str, Any] = {"selector": selector}
        if attributes:
            params["attributes"] = attributes
        data = await self._rtc.send_command("browser.query", params)
        return parse_result(data, QueryResult)

    async def query_all(self, selector: str, attributes: list[str] | None = None, limit: int = 20) -> QueryResult:
        self._check_active()
        params: dict[str, Any] = {"selector": selector, "limit": limit}
        if attributes:
            params["attributes"] = attributes
        data = await self._rtc.send_command("browser.query_all", params)
        return parse_result(data, QueryResult)

    async def get_html(self, selector: str = "html", outer: bool = True) -> HtmlResult:
        self._check_active()
        data = await self._rtc.send_command("browser.get_html", {"selector": selector, "outer": outer})
        return parse_result(data, HtmlResult)

    async def click(self, selector: str | None = None, x: int | None = None, y: int | None = None) -> None:
        self._check_active()
        await self._humanizer.before("click")
        params: dict[str, Any] = {}
        if selector:
            params["selector"] = selector
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        await self._rtc.send_command("browser.click", params)
        await self._humanizer.after("click")

    async def type(self, selector: str, text: str, delay_ms: int = 0) -> None:
        self._check_active()
        await self._humanizer.before("type")
        if self._human_profile:
            # Click to focus the element first
            await self._rtc.send_command("browser.click", {"selector": selector})
            # Per-char typing with jitter
            async for char, char_delay in self._humanizer.humanize_text(text):
                await self._rtc.send_command("keyboard.press", {
                    "session_id": self._session_id,
                    "key": char,
                })
                if char_delay > 0:
                    await asyncio.sleep(char_delay / 1000)
        else:
            await self._rtc.send_command("browser.type", {
                "selector": selector, "text": text, "delay_ms": delay_ms,
            })
        await self._humanizer.after("type")

    async def scroll(
        self,
        selector: str | None = None,
        direction: str = "down",
        amount: int = 500,
    ) -> None:
        self._check_active()
        await self._humanizer.before("scroll")
        params: dict[str, Any] = {}
        if selector:
            params["selector"] = selector
        else:
            params["direction"] = direction
            params["amount"] = amount
        await self._rtc.send_command("browser.scroll", params)
        await self._humanizer.after("scroll")

    async def screenshot(self, format: str = "png", quality: int = 80) -> ScreenshotResult:
        self._check_active()
        await self._humanizer.before("screenshot")
        data = await self._rtc.send_command("browser.screenshot", {"format": format, "quality": quality})
        await self._humanizer.after("screenshot")
        return parse_result(data, ScreenshotResult)

    async def back(self) -> NavigateResult:
        self._check_active()
        data = await self._rtc.send_command("browser.back")
        return parse_result(data, NavigateResult)

    async def forward(self) -> NavigateResult:
        self._check_active()
        data = await self._rtc.send_command("browser.forward")
        return parse_result(data, NavigateResult)

    async def reload(self) -> NavigateResult:
        self._check_active()
        data = await self._rtc.send_command("browser.reload")
        return parse_result(data, NavigateResult)

    def on_tab_opened(self, callback: Callable[[dict[str, Any]], Any]) -> None:
        """Register a listener for new tab events. Params dict has: session_id, tab_id, url, opener_tab_id."""
        self._tab_opened_callback = callback

    async def switch_tab(self, tab_id: int) -> dict[str, Any]:
        self._check_active()
        data = await self._rtc.send_command("tabs.switch", {"session_id": self._session_id, "tab_id": tab_id})
        return data if isinstance(data, dict) else {}

    async def close_tab(self, tab_id: int) -> dict[str, Any]:
        self._check_active()
        data = await self._rtc.send_command("tabs.close", {"session_id": self._session_id, "tab_id": tab_id})
        return data if isinstance(data, dict) else {}

    async def mouse_click(self, x: float, y: float, button: str = "left") -> None:
        self._check_active()
        await self._rtc.send_command("mouse.click", {"session_id": self._session_id, "x": x, "y": y, "button": button})

    async def mouse_move(self, x: float, y: float) -> None:
        self._check_active()
        await self._rtc.send_command("mouse.move", {"session_id": self._session_id, "x": x, "y": y})

    async def click_real(self, selector: str) -> dict[str, Any]:
        self._check_active()
        await self._humanizer.before("click")
        data = await self._rtc.send_command("mouse.click_selector", {"session_id": self._session_id, "selector": selector})
        await self._humanizer.after("click")
        return data if isinstance(data, dict) else {}

    async def key_press(self, key: str) -> None:
        self._check_active()
        await self._rtc.send_command("keyboard.press", {"session_id": self._session_id, "key": key})

    async def inject_credentials(self, secret_id: str, target: dict[str, str]) -> dict[str, Any]:
        self._check_active()
        params = {"secret_id": secret_id, **target}
        data = await self._rtc.send_command("browser.inject_credentials", params)
        return data if isinstance(data, dict) else {}

    async def request_human_action(
        self,
        action_type: str,
        message: str,
        timeout_sec: int = 120,
    ) -> HumanActionResult:
        self._check_active()
        import uuid

        data = await self._rtc.send_command(
            "browser.request_human_action",
            {
                "request_id": str(uuid.uuid4()),
                "type": action_type,
                "message": message,
                "timeout_sec": timeout_sec,
            },
            timeout=timeout_sec + 10,
        )
        return parse_result(data, HumanActionResult)

    def chat_direct(
        self,
        topic_id: str | None = None,
        chat_service_url: str = DEFAULT_CHAT_SERVICE_URL,
    ) -> ChatClient:
        tid = topic_id or getattr(self, "chat_topic_id", None)
        if not tid:
            raise CekiBrowserError(
                "topic_id required: pass it explicitly or set session.chat_topic_id"
            )
        token = self._transport._token
        self._chat_direct = ChatClient(
            token=token,
            topic_id=tid,
            base_url=chat_service_url,
        )
        return self._chat_direct

    async def end(self, reason: str = "completed") -> None:
        if not self._active:
            return
        self._active = False
        try:
            await self._transport.send(
                "session.end",
                {"session_id": self._session_id or self._request_id, "reason": reason},
                timeout=10,
            )
        except CekiBrowserError:
            pass
        if self._chat_direct:
            await self._chat_direct.close()
            self._chat_direct = None
        if self._rtc:
            await self._rtc.close()
            self._rtc = None
            self._chat = None

    def _check_active(self) -> None:
        if not self._active:
            raise CekiBrowserError("Session is not active")
        if not self._rtc:
            raise CekiBrowserError("P2P transport not established")

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.end()

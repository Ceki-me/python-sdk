from __future__ import annotations

import asyncio
from typing import Any

from .chat import ChatAPI
from .errors import CekiBrowserError
from .transport import Transport
from .types import (
    HtmlResult,
    HumanActionResult,
    NavigateResult,
    QueryResult,
    ScreenshotResult,
    parse_result,
)


class Session:
    def __init__(
        self,
        transport: Transport,
        request_id: str,
        mode: str,
    ):
        self._transport = transport
        self._request_id = request_id
        self._session_id: str | None = None
        self._mode = mode
        self._active = False
        self._chat: ChatAPI | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def active(self) -> bool:
        return self._active

    @property
    def chat(self) -> ChatAPI:
        if self._chat is None:
            self._chat = ChatAPI(
                self._transport,
                self._session_id or self._request_id,
                None,
            )
        return self._chat

    async def _wait_for_active(self, timeout: float = 60.0) -> None:
        ready = asyncio.Event()
        session_id_holder: list[str] = []

        original_cb = self._transport._event_callback

        async def _on_event(method: str, params: dict[str, Any]) -> None:
            if method == "session.state_changed":
                state = params.get("state")
                sid = params.get("session_id", params.get("request_id", ""))
                if state == "ACTIVE":
                    session_id_holder.append(sid)
                    ready.set()
                elif state in ("ENDED", "ENDING"):
                    ready.set()
            if method == "session.started":
                sid = params.get("session_id", "")
                session_id_holder.append(sid)
                ready.set()
            if original_cb:
                result = original_cb(method, params)
                if asyncio.iscoroutine(result):
                    await result

        self._transport.on_event(_on_event)
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise CekiBrowserError("Timed out waiting for session to become active")
        finally:
            self._transport.on_event(original_cb)

        if session_id_holder:
            self._session_id = session_id_holder[0]
        self._active = True

        self._chat = ChatAPI(
            self._transport,
            self._session_id or self._request_id,
            None,
        )
        self._install_chat_event_handler()

    async def navigate(self, url: str, timeout_ms: int = 120000) -> NavigateResult:
        self._check_active()
        data = await self._transport.send(
            "browser.navigate",
            {"url": url, "timeout_ms": timeout_ms},
            timeout=timeout_ms / 1000 + 5,
        )
        return parse_result(data, NavigateResult)

    async def query(self, selector: str, attributes: list[str] | None = None) -> QueryResult:
        self._check_active()
        params: dict[str, Any] = {"selector": selector}
        if attributes:
            params["attributes"] = attributes
        data = await self._transport.send("browser.query", params)
        return parse_result(data, QueryResult)

    async def query_all(self, selector: str, attributes: list[str] | None = None, limit: int = 20) -> QueryResult:
        self._check_active()
        params: dict[str, Any] = {"selector": selector, "limit": limit}
        if attributes:
            params["attributes"] = attributes
        data = await self._transport.send("browser.query_all", params)
        return parse_result(data, QueryResult)

    async def get_html(self, selector: str = "html", outer: bool = True) -> HtmlResult:
        self._check_active()
        data = await self._transport.send("browser.get_html", {"selector": selector, "outer": outer})
        return parse_result(data, HtmlResult)

    async def click(self, selector: str | None = None, x: int | None = None, y: int | None = None) -> None:
        self._check_active()
        params: dict[str, Any] = {}
        if selector:
            params["selector"] = selector
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        await self._transport.send("browser.click", params)

    async def type(self, selector: str, text: str, delay_ms: int = 0) -> None:
        self._check_active()
        await self._transport.send("browser.type", {"selector": selector, "text": text, "delay_ms": delay_ms})

    async def scroll(
        self,
        selector: str | None = None,
        direction: str = "down",
        amount: int = 500,
    ) -> None:
        self._check_active()
        params: dict[str, Any] = {}
        if selector:
            params["selector"] = selector
        else:
            params["direction"] = direction
            params["amount"] = amount
        await self._transport.send("browser.scroll", params)

    async def screenshot(self, format: str = "png", quality: int = 80) -> ScreenshotResult:
        self._check_active()
        data = await self._transport.send("browser.screenshot", {"format": format, "quality": quality})
        return parse_result(data, ScreenshotResult)

    async def back(self) -> NavigateResult:
        self._check_active()
        data = await self._transport.send("browser.back")
        return parse_result(data, NavigateResult)

    async def forward(self) -> NavigateResult:
        self._check_active()
        data = await self._transport.send("browser.forward")
        return parse_result(data, NavigateResult)

    async def reload(self) -> NavigateResult:
        self._check_active()
        data = await self._transport.send("browser.reload")
        return parse_result(data, NavigateResult)

    async def inject_credentials(self, secret_id: str, target: dict[str, str]) -> dict[str, Any]:
        self._check_active()
        params = {"secret_id": secret_id, **target}
        data = await self._transport.send("browser.inject_credentials", params)
        return data if isinstance(data, dict) else {}

    async def request_human_action(
        self,
        action_type: str,
        message: str,
        timeout_sec: int = 120,
    ) -> HumanActionResult:
        self._check_active()
        import uuid

        data = await self._transport.send(
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

    def _install_chat_event_handler(self) -> None:
        original_cb = self._transport._event_callback

        async def _on_event(method: str, params: dict[str, Any]) -> None:
            if method == "chat.topic_created" and self._chat:
                topic_id = params.get("chat_topic_id", "")
                if topic_id:
                    self._chat._set_topic_id(topic_id)
            elif method == "chat.message" and self._chat:
                self._chat._dispatch_message(params)
            elif method == "chat.typing" and self._chat:
                self._chat._dispatch_typing(params)

            if original_cb:
                result = original_cb(method, params)
                if asyncio.iscoroutine(result):
                    await result

        self._transport.on_event(_on_event)

    def _check_active(self) -> None:
        if not self._active:
            raise CekiBrowserError("Session is not active")

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.end()

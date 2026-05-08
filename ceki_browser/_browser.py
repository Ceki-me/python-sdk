from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, cast

if TYPE_CHECKING:
    from ._client import Client
from ._exceptions import (
    CdpUnrecoverable,
    InsufficientFunds,
    ProviderDisconnected,
    RateLimitExceeded,
    SessionEnded,
)
from ._models import Match

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
TabOpenedCallback = Callable[[str], Awaitable[None]]
SimpleCallback = Callable[[], Awaitable[None]]

_ERROR_TERMINAL = {-1011, -1012, -1015, -1018}


class Browser:
    def __init__(self, client: "Client", match: Match) -> None:
        self._client = client
        self._match = match
        self._cdp_counter = 0
        self._pending_cdp: dict[int, asyncio.Future[Any]] = {}
        self._event_callbacks: list[EventCallback] = []
        self._tab_opened_callbacks: list[TabOpenedCallback] = []
        self._provider_disconnected_callbacks: list[SimpleCallback] = []
        self._provider_reconnected_callbacks: list[SimpleCallback] = []
        self._ended = asyncio.Event()
        self._ended_reason: str | None = None

        from ._chat import BrowserChat
        from ._profile import BrowserProfile

        self.chat = BrowserChat(self)
        self.profile = BrowserProfile(self)

    @property
    def session_id(self) -> str:
        return self._match.session_id

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

    async def wait_until_ended(self) -> str:
        await self._ended.wait()
        return self._ended_reason or "unknown"

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

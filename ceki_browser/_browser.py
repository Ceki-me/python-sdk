from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._client import Client
from ._models import Match


class Browser:
    def __init__(self, client: "Client", match: Match) -> None:
        self._client = client
        self._match = match
        self._cdp_pending: dict[int, asyncio.Future[Any]] = {}
        self._cdp_events: list[Any] = []
        self._closed = False
        self._session_end_event: asyncio.Event = asyncio.Event()

    @property
    def session_id(self) -> str:
        return self._match.session_id

    @property
    def schedule_id(self) -> int:
        return self._match.schedule_id

    def _on_cdp_response(self, msg: dict[str, Any]) -> None:
        cmd_id = msg.get("id")
        if cmd_id is not None and cmd_id in self._cdp_pending:
            fut = self._cdp_pending.pop(cmd_id)
            if not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    err = msg.get("error", {})
                    fut.set_exception(Exception(f"CDP error {err}"))

    def _on_cdp_event(self, msg: dict[str, Any]) -> None:
        self._cdp_events.append(msg)

    def _on_tab_opened(self, msg: dict[str, Any]) -> None:
        pass

    def _on_session_ended(self, msg: dict[str, Any]) -> None:
        self._closed = True
        self._session_end_event.set()
        for fut in self._cdp_pending.values():
            if not fut.done():
                fut.cancel()
        self._cdp_pending.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._client._ws_send(
                {"type": "session.end", "session_id": self.session_id, "reason": "user_stop"}
            )
        except Exception:
            pass
        self._client._active_browsers.pop(self.session_id, None)

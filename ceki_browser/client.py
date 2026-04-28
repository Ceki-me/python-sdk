from __future__ import annotations

from typing import Any

from .errors import CekiBrowserError
from .session import Session
from .transport import DEFAULT_RELAY_URL, Transport


class Browser:
    def __init__(self, token: str, relay_url: str = DEFAULT_RELAY_URL):
        self._transport = Transport(token=token, relay_url=relay_url)
        self._connected = False

    @property
    def agent_id(self) -> str | None:
        return self._transport.agent_id

    @property
    def connected(self) -> bool:
        return self._connected and self._transport.connected

    def on_event(self, callback: Any) -> None:
        self._transport.on_event(callback)

    async def connect(self) -> dict[str, Any]:
        result = await self._transport.connect()
        self._connected = True
        return result

    async def close(self) -> None:
        self._connected = False
        await self._transport.close()

    async def session(
        self,
        mode: str = "incognito",
        domain_hints: list[str] | None = None,
        geo: str = "",
        language: str = "",
        max_price_per_min: float = 1.0,
        estimated_duration_min: int = 30,
        wait_timeout: float = 60.0,
    ) -> Session:
        if not self._connected:
            raise CekiBrowserError("Not connected. Call connect() or use `async with Browser(...)`")

        params: dict[str, Any] = {
            "mode": mode,
            "max_price_per_min": max_price_per_min,
            "estimated_duration_min": estimated_duration_min,
        }
        if domain_hints:
            params["domain_hints"] = domain_hints
        if geo:
            params["geo"] = geo
        if language:
            params["language"] = language

        result = await self._transport.send("session.request", params, timeout=30)
        request_id = result.get("request_id", "") if isinstance(result, dict) else ""

        sess = Session(self._transport, request_id, mode)
        await sess._wait_for_active(timeout=wait_timeout)
        return sess

    async def __aenter__(self) -> Browser:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

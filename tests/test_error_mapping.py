from __future__ import annotations

import asyncio

import pytest
import websockets
import websockets.server

from ceki_browser import ConnectOptions, connect
from ceki_browser._exceptions import AuthFailed, ProviderOffline, SessionEnded


class _CloseImmediately4403:
    """WS server that accepts upgrade then immediately closes with 4403."""

    def __init__(self) -> None:
        self._server: websockets.server.WebSocketServer | None = None
        self.port: int = 0

    @staticmethod
    def _select_subprotocol(ws: websockets.server.WebSocketServerProtocol, subprotocols: list[str]) -> str | None:
        for sp in subprotocols:
            if sp.startswith("bearer."):
                return sp
        return None

    async def _handler(self, ws: websockets.server.WebSocketServerProtocol) -> None:
        await ws.close(4403, "unauthorized")

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            "127.0.0.1",
            0,
            select_subprotocol=self._select_subprotocol,
        )
        self.port = next(iter(self._server.sockets)).getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def close_4403_server():
    server = _CloseImmediately4403()
    await server.start()
    yield server
    await server.stop()


@pytest.mark.asyncio
async def test_connect_bogus_token_close_4403_raises_auth_failed(close_4403_server: _CloseImmediately4403) -> None:
    """Relay accepts WS upgrade then immediately closes 4403 → connect() raises AuthFailed within 2s."""
    url = f"ws://127.0.0.1:{close_4403_server.port}"
    with pytest.raises(AuthFailed):
        await asyncio.wait_for(connect("bad-token", ConnectOptions(relay_url=url)), timeout=2.0)


@pytest.mark.asyncio
async def test_handle_error_minus_1015_raises_provider_offline(mock_relay) -> None:
    """error code=-1015 from relay → ProviderOffline raised from rent()."""
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(schedule_id=99))
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({"type": "error", "code": -1015, "reason": "no_providers"})

    with pytest.raises(ProviderOffline) as exc_info:
        await asyncio.wait_for(rent_task, timeout=5)
    assert "no_providers" in str(exc_info.value)

    await client.close()


@pytest.mark.asyncio
async def test_error_message_uses_reason_field(mock_relay) -> None:
    """relay sends {code:-1011, reason:'heartbeat_timeout'} with no 'message' field
    → SessionEnded.reason == 'heartbeat_timeout', not 'None' or 'ended'."""
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(schedule_id=77))
    await asyncio.sleep(0.05)

    # error with reason field only (no message), no session_id → goes to _handle_error
    await mock_relay.send_to_all({
        "type": "error",
        "code": -1011,
        "reason": "heartbeat_timeout",
    })

    with pytest.raises(SessionEnded) as exc_info:
        await asyncio.wait_for(rent_task, timeout=5)
    assert exc_info.value.reason == "heartbeat_timeout"
    assert exc_info.value.reason != "None"

    await client.close()

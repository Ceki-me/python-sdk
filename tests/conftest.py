from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import websockets
import websockets.server


class MockRelayServer:
    def __init__(self) -> None:
        self.connections: list[websockets.server.WebSocketServerProtocol] = []
        self.received: list[dict[str, Any]] = []
        self._server: websockets.server.WebSocketServer | None = None
        self.port: int = 0

    @staticmethod
    def _select_subprotocol(
        ws: websockets.server.WebSocketServerProtocol, subprotocols: list[str]
    ) -> str | None:
        for sp in subprotocols:
            if sp.startswith("bearer."):
                return sp
        return None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            "127.0.0.1",
            0,  # OS assigns port
            select_subprotocol=self._select_subprotocol,
        )
        self.port = next(iter(self._server.sockets)).getsockname()[1]

    async def _handler(self, ws: websockets.server.WebSocketServerProtocol) -> None:
        self.connections.append(ws)
        try:
            async for raw in ws:
                msg: dict[str, Any] = json.loads(raw)
                self.received.append(msg)
                if msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)

    async def send_to_all(self, msg: dict[str, Any]) -> None:
        for ws in list(self.connections):
            await ws.send(json.dumps(msg))

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def mock_relay() -> AsyncGenerator[MockRelayServer, None]:
    server = MockRelayServer()
    await server.start()
    yield server
    await server.stop()

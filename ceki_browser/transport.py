from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection

from .errors import (
    ERROR_CODE_MAP,
    AuthError,
    CekiBrowserError,
    CommandTimeout,
)

logger = logging.getLogger("ceki_browser")

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]

DEFAULT_RELAY_URL = "wss://browser.ceki.me/ws/agent"

MAX_RECONNECT_ATTEMPTS = 5
BASE_RECONNECT_DELAY = 1.0


class Transport:
    def __init__(self, token: str, relay_url: str = DEFAULT_RELAY_URL):
        self._token = token
        self._relay_url = relay_url
        self._ws: ClientConnection | None = None
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._event_callback: EventCallback | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._agent_id: str | None = None
        self._closed = False

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    def on_event(self, callback: EventCallback) -> None:
        self._event_callback = callback

    async def connect(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            self._ws = await websockets.connect(self._relay_url, additional_headers=headers)
        except Exception as e:
            raise AuthError(f"Failed to connect to relay: {e}", code=401) from e

        welcome_raw = await self._ws.recv()
        welcome = json.loads(welcome_raw)

        if "error" in welcome:
            err = welcome["error"]
            raise AuthError(err.get("message", "Authentication failed"), code=err.get("code", 401))

        result = welcome.get("result", {})
        self._agent_id = result.get("agent_id")
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return result

    async def close(self) -> None:
        self._closed = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        if not self._ws:
            raise CekiBrowserError("Not connected")

        msg_id = self._next_id
        self._next_id += 1

        payload = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params:
            payload["params"] = params

        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut

        await self._ws.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise CommandTimeout(f"Command {method} timed out after {timeout}s", code=-1020)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self._ws:
            raise CekiBrowserError("Not connected")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._ws.send(json.dumps(payload))

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")

                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "error" in msg:
                        err = msg["error"]
                        code = err.get("code", 0)
                        message = err.get("message", "Unknown error")
                        exc_cls = ERROR_CODE_MAP.get(code, CekiBrowserError)
                        fut.set_exception(exc_cls(message, code=code))
                    else:
                        fut.set_result(msg.get("result"))
                elif "method" in msg:
                    if self._event_callback:
                        result = self._event_callback(msg["method"], msg.get("params", {}))
                        if asyncio.iscoroutine(result):
                            await result
        except websockets.ConnectionClosed:
            logger.info("WebSocket connection closed")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("recv loop error: %s", e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CekiBrowserError("Connection lost"))
            self._pending.clear()

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(10)
                if self._ws and not self._closed:
                    try:
                        await self.send("heartbeat", timeout=5.0)
                    except (CekiBrowserError, asyncio.CancelledError):
                        break
        except asyncio.CancelledError:
            return

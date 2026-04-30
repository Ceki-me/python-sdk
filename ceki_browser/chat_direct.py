from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

import aiohttp
import websockets

logger = logging.getLogger("ceki_browser")

DEFAULT_CHAT_SERVICE_URL = os.environ.get(
    "CEKI_CHAT_SERVICE_URL", "https://chat.ceki.me"
)

MAX_RECONNECT_ATTEMPTS = 10
BASE_RECONNECT_DELAY = 1.0


class ChatClient:
    def __init__(
        self,
        token: str,
        topic_id: str,
        base_url: str = DEFAULT_CHAT_SERVICE_URL,
    ):
        self._token = token
        self._topic_id = topic_id
        self._base_url = base_url.rstrip("/")
        self._ws_url = self._base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        self._last_known_id: str | None = None
        self._ws: Any = None
        self._listen_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def topic_id(self) -> str:
        return self._topic_id

    async def history(
        self,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": str(limit)}
        if after:
            params["after"] = after
        elif before:
            params["before"] = before

        url = f"{self._base_url}/api/chat/topics/{self._topic_id}/messages"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"chat-service returned {resp.status}: {text}")
                data = await resp.json()

        msgs = data.get("messages", [])
        if msgs:
            self._last_known_id = msgs[-1].get("_id") or self._last_known_id
        return msgs

    async def send(self, body: str, msg_type: str = "text") -> dict[str, Any]:
        url = f"{self._base_url}/api/chat/topics/{self._topic_id}/messages"
        payload = {"type": msg_type, "content": body}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"chat-service returned {resp.status}: {text}")
                return await resp.json()

    async def subscribe(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> None:
        if self._listen_task and not self._listen_task.done():
            return

        if not self._last_known_id:
            msgs = await self.history(limit=1)
            if msgs:
                self._last_known_id = msgs[-1].get("_id")

        self._listen_task = asyncio.get_event_loop().create_task(
            self._ws_loop(on_message)
        )

    async def _ws_loop(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> None:
        attempt = 0
        while not self._closed:
            try:
                self._ws = await websockets.connect(self._ws_url)
                attempt = 0

                await self._ws.send(json.dumps({
                    "action": "auth",
                    "token": f"Bearer {self._token}",
                }))
                auth_resp = json.loads(await self._ws.recv())
                if auth_resp.get("type") == "error":
                    logger.error("WS auth failed: %s", auth_resp)
                    break

                await self._ws.send(json.dumps({
                    "action": "subscribe",
                    "topic_id": self._topic_id,
                }))
                sub_resp = json.loads(await self._ws.recv())
                logger.debug("WS subscribe response: %s", sub_resp)

                if self._last_known_id:
                    missed = await self.history(after=self._last_known_id, limit=200)
                    for msg in missed:
                        result = on_message(msg)
                        if asyncio.iscoroutine(result):
                            await result

                async for raw in self._ws:
                    if self._closed:
                        break
                    event = json.loads(raw)
                    if event.get("event") == "message":
                        msg = event.get("message", {})
                        msg_topic = str(msg.get("topic_id", ""))
                        if msg_topic == self._topic_id:
                            msg_id = str(msg.get("_id", ""))
                            if msg_id:
                                self._last_known_id = msg_id
                            result = on_message(msg)
                            if asyncio.iscoroutine(result):
                                await result

            except (websockets.ConnectionClosed, OSError) as e:
                if self._closed:
                    break
                attempt += 1
                if attempt > MAX_RECONNECT_ATTEMPTS:
                    logger.error("WS reconnect limit reached (%d)", MAX_RECONNECT_ATTEMPTS)
                    break
                delay = min(BASE_RECONNECT_DELAY * (2 ** (attempt - 1)), 30)
                logger.warning("WS disconnected (%s), reconnecting in %.1fs (attempt %d)", e, delay, attempt)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def close(self) -> None:
        self._closed = True
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None

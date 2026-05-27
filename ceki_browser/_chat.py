from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Coroutine, cast
from uuid import uuid4

import httpx

from ._exceptions import ChatSendFailed
from ._models import ChatMessage, ReadReceipt

if TYPE_CHECKING:
    from ._browser import Browser

log = logging.getLogger(__name__)

MessageCallback = Callable[[ChatMessage], Awaitable[None]]
ReadCallback = Callable[[ReadReceipt], Awaitable[None]]

MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _detect_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class BrowserChat:
    def __init__(self, browser: "Browser") -> None:
        self._browser = browser
        self._topic_id: str | None = browser.chat_topic_id
        self._message_callbacks: list[MessageCallback] = []
        self._read_callbacks: list[ReadCallback] = []
        self._pending_sends: dict[str, asyncio.Future[dict]] = {}
        self._action_queues: dict[int, asyncio.Queue[dict]] = {}

    async def send(self, text: str) -> dict:
        if not self._topic_id:
            raise RuntimeError("chat topic not assigned (rent did not return chat_topic_id)")
        client_msg_id = uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending_sends[client_msg_id] = fut
        try:
            await self._browser._client._ws_send(
                {
                    "type": "chat.send",
                    "session_id": self._browser.session_id,
                    "client_msg_id": client_msg_id,
                    "text": text,
                }
            )
            return await asyncio.wait_for(asyncio.shield(fut), timeout=15)
        finally:
            self._pending_sends.pop(client_msg_id, None)

    async def send_image(
        self,
        image: bytes | str | Path,
        *,
        mime: str | None = None,
        filename: str | None = None,
    ) -> dict:
        if not self._topic_id:
            raise RuntimeError("chat topic not assigned (rent did not return chat_topic_id)")

        if isinstance(image, (str, Path)):
            path = Path(image)
            data = path.read_bytes()
            if mime is None:
                guessed, _ = mimetypes.guess_type(str(path))
                mime = guessed or _detect_mime(data)
            if filename is None:
                filename = path.name
        else:
            data = image
            if mime is None:
                mime = _detect_mime(data)
            if filename is None:
                ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(mime or '', 'bin')
                filename = f'image-{uuid4().hex[:8]}.{ext}'

        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large, max 5MB (got {len(data)} bytes)")

        b64 = base64.b64encode(data).decode()
        client_msg_id = uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending_sends[client_msg_id] = fut
        try:
            await self._browser._client._ws_send(
                {
                    "type": "chat.send_image",
                    "session_id": self._browser.session_id,
                    "client_msg_id": client_msg_id,
                    "filename": filename,
                    "mime": mime,
                    "data_b64": b64,
                }
            )
            return await asyncio.wait_for(asyncio.shield(fut), timeout=15)
        finally:
            self._pending_sends.pop(client_msg_id, None)

    def on_message(self, callback: MessageCallback) -> None:
        self._message_callbacks.append(callback)

    def on_read(self, callback: ReadCallback) -> None:
        self._read_callbacks.append(callback)

    async def history(
        self,
        limit: int = 50,
        before_id: int | None = None,
        since: str | None = None,
    ) -> list[ChatMessage]:
        if not self._topic_id:
            return []
        client = self._browser._client
        base = client.chat_url.rstrip('/')
        params: dict = {"topic_id": self._topic_id, "limit": limit}
        if before_id is not None:
            params["before"] = before_id
        if since is not None:
            params["since"] = since
        req = httpx.Request(
            "GET",
            f"{base}/messages",
            headers={"Authorization": f"Bearer {client.api_key}"},
            params=params,
        )
        async with httpx.AsyncClient() as http:
            resp = await http.send(req)
            resp.raise_for_status()
        data = resp.json()
        items = data.get("messages", data.get("data", data)) if isinstance(data, dict) else data
        return [ChatMessage.model_validate(m) for m in items]

    async def _on_message(self, payload: dict) -> None:
        raw = payload.get("message")
        if not isinstance(raw, dict):
            log.warning("chat.message without nested 'message': %s", payload)
            return
        if raw.get("type") == "action" and isinstance(raw.get("action"), dict):
            action = raw["action"]
            eid = action.get("event_id")
            if eid is not None:
                queue = self._action_queues.get(int(eid))
                if queue is not None:
                    await queue.put(action)
        try:
            msg = ChatMessage.model_validate(raw)
        except Exception as exc:
            log.warning("invalid chat.message payload: %s", exc)
            return
        for cb in self._message_callbacks:
            try:
                asyncio.create_task(cast(Coroutine, cb(msg)))
            except Exception as exc:
                log.warning("chat on_message callback error: %s", exc)

    async def _on_read(self, payload: dict) -> None:
        try:
            receipt = ReadReceipt.model_validate(payload)
        except Exception as exc:
            log.warning("invalid chat.read payload: %s", exc)
            return
        for cb in self._read_callbacks:
            try:
                asyncio.create_task(cast(Coroutine, cb(receipt)))
            except Exception as exc:
                log.warning("chat on_read callback error: %s", exc)

    async def _on_send_ack(self, msg: dict) -> None:
        client_msg_id = msg.get("client_msg_id", "")
        if client_msg_id in self._pending_sends:
            fut = self._pending_sends.pop(client_msg_id)
            if not fut.done():
                fut.set_result(
                    {
                        "message_id": msg.get("message_id"),
                        "sent_at": msg.get("sent_at"),
                    }
                )

    async def _on_send_error(self, msg: dict) -> None:
        client_msg_id = msg.get("client_msg_id", "")
        status = msg.get("status", 0)
        message = msg.get("message", "unknown")
        if client_msg_id and client_msg_id in self._pending_sends:
            fut = self._pending_sends.pop(client_msg_id)
            if not fut.done():
                fut.set_exception(ChatSendFailed(status, message))

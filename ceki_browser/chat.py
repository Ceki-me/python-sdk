from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Callable

from .transport import Transport
from .types import ChatMessage, TypingEvent, parse_chat_message

logger = logging.getLogger("ceki_browser")

Unsubscribe = Callable[[], None]


class ChatAPI:
    def __init__(self, transport: Transport, session_id: str, topic_id: str | None):
        self._transport = transport
        self._session_id = session_id
        self._topic_id = topic_id
        self._message_handlers: list[Callable[[ChatMessage], None]] = []
        self._typing_handlers: list[Callable[[TypingEvent], None]] = []

    @property
    def topic_id(self) -> str | None:
        return self._topic_id

    @property
    def available(self) -> bool:
        return self._topic_id is not None

    def _set_topic_id(self, topic_id: str) -> None:
        self._topic_id = topic_id

    async def send(self, text: str) -> ChatMessage:
        data = await self._transport.send(
            "chat.send",
            {"session_id": self._session_id, "type": "text", "content": text},
            timeout=15.0,
        )
        result = data if isinstance(data, dict) else {}
        return ChatMessage(
            _id=result.get("message_id", ""),
            topic_id=self._topic_id or "",
            author_id=0,
            author_name="",
            type="text",
            content=text,
            media=None,
            created_at=result.get("created_at", ""),
        )

    async def send_image(
        self,
        image: bytes | Path | str,
        mime: str = "image/png",
    ) -> ChatMessage:
        if isinstance(image, (str, Path)):
            path = Path(image)
            raw = path.read_bytes()
            ext = path.suffix.lower()
            if ext in (".jpg", ".jpeg"):
                mime = "image/jpeg"
            elif ext == ".gif":
                mime = "image/gif"
            elif ext == ".webp":
                mime = "image/webp"
        else:
            raw = image

        b64 = base64.b64encode(raw).decode("ascii")
        name = f"image.{mime.split('/')[-1]}"

        data = await self._transport.send(
            "chat.send",
            {
                "session_id": self._session_id,
                "type": "image",
                "content": "",
                "media": {"data": b64, "mime": mime, "name": name},
            },
            timeout=30.0,
        )
        result = data if isinstance(data, dict) else {}
        return ChatMessage(
            _id=result.get("message_id", ""),
            topic_id=self._topic_id or "",
            author_id=0,
            author_name="",
            type="image",
            content="",
            media=None,
            created_at=result.get("created_at", ""),
        )

    async def history(
        self,
        before: str | None = None,
        limit: int = 50,
    ) -> list[ChatMessage]:
        if not self._topic_id:
            logger.warning("chat.history called without topic_id — returning empty")
            return []

        params: dict[str, Any] = {"session_id": self._session_id, "limit": limit}
        if before:
            params["before"] = before

        data = await self._transport.send("chat.history", params, timeout=15.0)
        result = data if isinstance(data, dict) else {}
        messages = result.get("messages", [])
        return [parse_chat_message(m) for m in messages if isinstance(m, dict)]

    async def mark_read(self, last_message_id: str) -> None:
        if not self._topic_id:
            return
        await self._transport.send(
            "chat.read",
            {"session_id": self._session_id, "last_message_id": last_message_id},
            timeout=10.0,
        )

    async def typing(self, is_typing: bool = True) -> None:
        await self._transport.notify(
            "chat.typing",
            {"session_id": self._session_id, "is_typing": is_typing},
        )

    def on_message(self, handler: Callable[[ChatMessage], None]) -> Unsubscribe:
        self._message_handlers.append(handler)

        def unsub() -> None:
            try:
                self._message_handlers.remove(handler)
            except ValueError:
                pass

        return unsub

    def on_typing(self, handler: Callable[[TypingEvent], None]) -> Unsubscribe:
        self._typing_handlers.append(handler)

        def unsub() -> None:
            try:
                self._typing_handlers.remove(handler)
            except ValueError:
                pass

        return unsub

    def _dispatch_message(self, params: dict[str, Any]) -> None:
        msg_data = params.get("message", params)
        if isinstance(msg_data, dict):
            msg = parse_chat_message(msg_data)
            for h in self._message_handlers:
                try:
                    h(msg)
                except Exception:
                    logger.exception("Error in chat message handler")

    def _dispatch_typing(self, params: dict[str, Any]) -> None:
        event = TypingEvent(
            user_id=params.get("user_id", 0),
            is_typing=bool(params.get("is_typing", False)),
        )
        for h in self._typing_handlers:
            try:
                h(event)
            except Exception:
                logger.exception("Error in chat typing handler")

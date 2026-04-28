from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    elements: list[dict[str, str | None]] = field(default_factory=list)

    @property
    def text(self) -> str | None:
        if self.elements:
            return self.elements[0].get("textContent")
        return None

    @property
    def value(self) -> str | None:
        if self.elements:
            return self.elements[0].get("value")
        return None

    def __len__(self) -> int:
        return len(self.elements)


@dataclass
class NavigateResult:
    url: str = ""
    title: str = ""
    status: int = 0


@dataclass
class ScreenshotResult:
    data: str = ""
    width: int = 0
    height: int = 0


@dataclass
class HtmlResult:
    html: str = ""


@dataclass
class SessionInfo:
    request_id: str = ""
    session_id: str = ""
    status: str = ""


@dataclass
class HumanActionResult:
    status: str = ""
    request_id: str = ""


@dataclass
class ChatMessage:
    _id: str = ""
    topic_id: str = ""
    author_id: int = 0
    author_name: str = ""
    type: str = "text"
    content: str = ""
    media: dict[str, Any] | None = None
    created_at: str = ""


@dataclass
class TypingEvent:
    user_id: int = 0
    is_typing: bool = False


def parse_chat_message(data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        _id=str(data.get("_id", data.get("message_id", data.get("id", "")))),
        topic_id=str(data.get("topic_id", "")),
        author_id=int(data.get("author_id", data.get("user_id", 0))),
        author_name=str(data.get("author_name", "")),
        type=str(data.get("type", "text")),
        content=str(data.get("content", "")),
        media=data.get("media"),
        created_at=str(data.get("created_at", "")),
    )


def parse_result(data: Any, cls: type) -> Any:
    if data is None:
        return cls()
    if isinstance(data, dict):
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in valid_fields})
    return cls()

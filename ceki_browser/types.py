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


def parse_result(data: Any, cls: type) -> Any:
    if data is None:
        return cls()
    if isinstance(data, dict):
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in valid_fields})
    return cls()

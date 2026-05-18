from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowserOption(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schedule_id: int
    user_id: int | None = None
    geo: str | None = None
    language: str | None = None
    languages: list[str] = []
    domain_allowed: list[str] | None = None
    skills: list[str] = []
    price_per_min: float
    rating: float | None = None
    online: bool = True
    currency: str | None = None
    kal_id: int | None = None
    profile_mode: Literal['main', 'incognito'] | None = None
    allowed_domains: list[str] | None = None


class Match(BaseModel):
    model_config = ConfigDict(extra='ignore')

    session_id: str
    schedule_id: int
    event_id: str | None = None
    chat_topic_id: str | None = None
    provider_user_id: int | None = None
    started_at: float = 0.0
    browser_info: dict = {}


class ChatMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra='ignore')

    id: str = Field(alias='_id')
    topic_id: str
    sender_id: int | None = None
    text: str | None = None
    media: list[dict] | None = None
    type: str = 'text'
    created_at: str
    edited_at: str | None = None
    deleted_at: str | None = None
    action: dict | None = None

    def is_system(self) -> bool:
        return self.type == 'system'

    def is_from_provider(self, provider_user_id: int | None) -> bool:
        return provider_user_id is not None and self.sender_id == provider_user_id


class ReadReceipt(BaseModel):
    model_config = ConfigDict(extra='ignore')

    topic_id: str
    last_read_message_id: str
    read_at: float = 0.0


class Snapshot(BaseModel):
    screenshot: str
    chat: list[ChatMessage] = []
    ts: datetime

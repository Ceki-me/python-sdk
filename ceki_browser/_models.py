from pydantic import BaseModel, ConfigDict


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


class Match(BaseModel):
    session_id: str
    schedule_id: int
    event_id: str | None = None
    chat_topic_id: int | None = None
    started_at: float = 0.0
    browser_info: dict = {}


class ChatMessage(BaseModel):
    message_id: int
    sender_type: str
    sender_id: int
    text: str | None = None
    image_url: str | None = None
    sent_at: float


class ReadReceipt(BaseModel):
    topic_id: int
    last_read_message_id: int
    read_at: float

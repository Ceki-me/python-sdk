from .chat_direct import ChatClient
from .client import Browser
from .errors import (
    AuthError,
    CekiBrowserError,
    CommandTimeout,
    HumanActionDeclined,
    HumanActionTimeout,
    NavigationTimeout,
    NoMatchError,
    ProviderDisconnected,
    ProviderNotVerified,
    RateLimited,
    SessionEndedError,
)
from .session import ChatAPI, Session
from .transport_rtc import ChatImage, ChatTextMessage, RTCTransport
from .types import (
    ChatMessage,
    HtmlResult,
    HumanActionResult,
    NavigateResult,
    QueryResult,
    ScreenshotResult,
    TypingEvent,
)

__all__ = [
    "Browser",
    "ChatAPI",
    "ChatClient",
    "Session",
    "RTCTransport",
    "ChatImage",
    "ChatTextMessage",
    "AuthError",
    "CekiBrowserError",
    "CommandTimeout",
    "HumanActionDeclined",
    "HumanActionTimeout",
    "NavigationTimeout",
    "ProviderDisconnected",
    "ProviderNotVerified",
    "NoMatchError",
    "RateLimited",
    "SessionEndedError",
    "ChatMessage",
    "HtmlResult",
    "HumanActionResult",
    "NavigateResult",
    "QueryResult",
    "ScreenshotResult",
    "TypingEvent",
]

__version__ = "0.2.0"

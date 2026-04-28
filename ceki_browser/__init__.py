from .client import Browser
from .errors import (
    AuthError,
    CekiBrowserError,
    CommandTimeout,
    HumanActionDeclined,
    HumanActionTimeout,
    NavigationTimeout,
    ProviderDisconnected,
    ProviderNotVerified,
    RateLimited,
)
from .chat import ChatAPI
from .session import Session
from .types import ChatMessage, HtmlResult, HumanActionResult, NavigateResult, QueryResult, ScreenshotResult, TypingEvent

__all__ = [
    "Browser",
    "ChatAPI",
    "Session",
    "AuthError",
    "CekiBrowserError",
    "CommandTimeout",
    "HumanActionDeclined",
    "HumanActionTimeout",
    "NavigationTimeout",
    "ProviderDisconnected",
    "ProviderNotVerified",
    "RateLimited",
    "ChatMessage",
    "HtmlResult",
    "HumanActionResult",
    "NavigateResult",
    "QueryResult",
    "ScreenshotResult",
    "TypingEvent",
]

__version__ = "0.1.0"

from ._browser import Browser
from ._client import Client
from ._connect import connect
from ._exceptions import (
    AuthFailed,
    CdpUnrecoverable,
    CekiError,
    ConnectionLost,
    InsufficientFunds,
    RateLimitExceeded,
    SessionEnded,
)
from ._models import BrowserOption, ChatMessage, Match, ReadReceipt

__version__ = "2.1.0"
__all__ = [
    "connect",
    "Client",
    "Browser",
    "BrowserOption",
    "Match",
    "ChatMessage",
    "ReadReceipt",
    "RateLimitExceeded",
    "InsufficientFunds",
    "SessionEnded",
    "CdpUnrecoverable",
    "AuthFailed",
    "ConnectionLost",
    "CekiError",
]

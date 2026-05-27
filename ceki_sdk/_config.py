import os

DEFAULT_API_URL   = "https://api.ceki.me"
DEFAULT_RELAY_URL = "wss://browser.ceki.me/ws/agent"


def default_api_url() -> str:
    return os.getenv("CEKI_API_URL") or DEFAULT_API_URL


def default_relay_url() -> str:
    return os.getenv("CEKI_RELAY_URL") or DEFAULT_RELAY_URL


DEFAULT_CHAT_URL = "https://chat.ceki.me/api/chat"


def default_chat_url() -> str:
    return os.getenv("CEKI_CHAT_URL") or DEFAULT_CHAT_URL

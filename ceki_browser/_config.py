import os

RELAY_URL_PROD = "wss://browser.ceki.me/ws/agent"
RELAY_URL_DEV = "wss://browser.ittribe.org/ws/agent"


def default_relay_url() -> str:
    if url := os.getenv("CEKI_RELAY_URL"):
        return url
    if os.getenv("CEKI_ENV", "").lower() == "dev":
        return RELAY_URL_DEV
    return RELAY_URL_PROD

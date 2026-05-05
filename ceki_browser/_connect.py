from __future__ import annotations

from ._client import Client
from ._config import default_relay_url


async def connect(
    api_key: str,
    *,
    reconnect: bool = True,
    relay_url: str | None = None,
) -> Client:
    url = relay_url or default_relay_url()
    client = Client(api_key=api_key, relay_url=url, reconnect=reconnect)
    await client._connect()
    return client

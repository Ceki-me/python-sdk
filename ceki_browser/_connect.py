from __future__ import annotations

from dataclasses import dataclass

from ._client import Client
from ._config import default_api_url, default_relay_url


@dataclass
class ConnectOptions:
    api_url: str | None = None
    relay_url: str | None = None
    basic_auth: tuple[str, str] | None = None
    reconnect: bool = True


async def connect(api_key: str, options: ConnectOptions | None = None) -> Client:
    options = options or ConnectOptions()
    relay_url = options.relay_url or default_relay_url()
    api_url = options.api_url or default_api_url()
    client = Client(
        api_key=api_key,
        relay_url=relay_url,
        api_url=api_url,
        reconnect=options.reconnect,
        basic_auth=options.basic_auth,
    )
    await client._connect()
    return client

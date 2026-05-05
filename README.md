# ceki-browser

Python SDK for [ceki.me](https://ceki.me) — rent real browsers from real people for AI agent automation.

## Install

```bash
pip install ceki-browser
```

## Quickstart

```python
import asyncio
import os
from ceki_browser import connect

async def main():
    client = await connect(os.environ["CEKI_API_KEY"])
    options = await client.search({"geo": "US", "language": "en"})
    browser = await client.rent(options[0].schedule_id)
    # ... CDP calls (see docs)
    await browser.close()
    await client.close()

asyncio.run(main())
```

## Environment Variables

| Variable | Description |
|---|---|
| `CEKI_API_KEY` | Your API key (required) |
| `CEKI_RELAY_URL` | Override relay WebSocket URL |
| `CEKI_ENV` | Set to `dev` to use dev relay |

## API

### `connect(api_key, *, reconnect=True, relay_url=None) -> Client`

Establish a WebSocket connection to the relay. Returns a `Client` instance.

### `client.search(filters=None, limit=20) -> list[BrowserOption]`

Search for available browsers. Filters: `geo`, `language`, etc.

### `client.rent(schedule_id, duration_minutes=60) -> Browser`

Rent a browser. Waits up to 60s for a match.

### `client.close()`

Close all sessions and the connection.

## Error Codes

| Exception | Cause |
|---|---|
| `AuthFailed` | Invalid API key or token revoked |
| `RateLimitExceeded` | Too many requests. Has `.retry_after` (seconds) |
| `InsufficientFunds` | Account balance too low |
| `SessionEnded` | Provider ended the session. Has `.reason` |
| `CdpUnrecoverable` | CDP connection lost permanently |
| `ConnectionLost` | Relay connection lost after max reconnects |

## CDP Lifecycle

The relay maintains the CDP connection to the incognito browser tab. If the connection drops, it automatically reattaches with 1s/2s/4s exponential backoff. Commands during reattach are buffered (FIFO, max 50). If 3 reattach attempts fail, a new fallback tab is created. If that also fails, `cdp_unrecoverable` error is sent.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check ceki_browser/
mypy ceki_browser/
```

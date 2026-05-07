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
from ceki_browser import connect, ConnectOptions

async def main():
    client = await connect(os.environ["CEKI_API_KEY"])
    options = await client.search({"geo": "US", "language": "en"})
    browser = await client.rent(options[0].schedule_id)
    # ... CDP calls (see docs)
    await browser.close()
    await client.close()

asyncio.run(main())
```

Dev / staging with basic-auth:

```python
client = await connect(
    os.environ["CEKI_API_KEY"],
    ConnectOptions(
        api_url="https://clawapi.ittribe.org",
        relay_url="wss://browser.ittribe.org/ws/agent",
        basic_auth=("admin", "clawdev"),
    ),
)
```

**BREAKING in 2.2.0:** `connect()` no longer accepts `relay_url=` or `reconnect=` kwargs — pass a `ConnectOptions` object instead.

## Environment Variables

| Variable | Description |
|---|---|
| `CEKI_API_KEY` | Your API key (required) |
| `CEKI_API_URL` | Override REST API base URL |
| `CEKI_RELAY_URL` | Override relay WebSocket URL |

## API

### `connect(api_key, options: ConnectOptions | None = None) -> Client`

Establish a WebSocket connection to the relay. Returns a `Client` instance.

### `ConnectOptions`

| Field | Default | Description |
|---|---|---|
| `api_url` | `https://api.ceki.me` | REST API base URL |
| `relay_url` | `wss://browser.ceki.me/ws/agent` | Relay WebSocket URL |
| `basic_auth` | `None` | `(user, password)` for nginx htpasswd |
| `reconnect` | `True` | Auto-reconnect on disconnect |

### `client.search(filters=None, limit=20) -> list[BrowserOption]`

Search for available browsers. Filters: `geo`, `language`, etc.

### `client.rent(schedule_id) -> Browser`

Rent a browser by schedule ID. Waits up to 60s for a match.

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

## Real-signup examples

See `examples/SMOKE.md` for full runbook.

Quick:
```bash
pip install -e ".[dev]"
export CEKI_API_KEY=...
export SCHEDULE_ID=...
python examples/reddit_signup.py
```

These are NOT automated tests — they require a live relay, an online provider, and a real IMAP mailbox. Run manually as part of Phase 2 acceptance.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check ceki_browser/
mypy ceki_browser/
```

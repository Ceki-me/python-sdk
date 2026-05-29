# ceki-sdk

Python SDK for [browser.ceki.me](https://browser.ceki.me) — rent real browsers from real people for AI agent automation.

## Install

```bash
pip install ceki-sdk
```

## Quickstart

```python
import asyncio
import os
from ceki_sdk import connect, ConnectOptions

async def main():
    client = await connect(os.environ["CEKI_API_KEY"])
    options = await client.search({"geo": "US", "language": "en"})
    browser = await client.rent(options[0].browser_id)
    # ... CDP calls (see docs)
    await browser.close()
    await client.close()

asyncio.run(main())
```

**BREAKING in 2.2.0:** `connect()` no longer accepts `relay_url=` or `reconnect=` kwargs — pass a `ConnectOptions` object instead.

## Environment Variables

| Variable | Description |
|---|---|
| `CEKI_API_KEY` | Your API key (required) |

## API

### `connect(api_key, options: ConnectOptions | None = None) -> Client`

Establish a WebSocket connection to the relay. Returns a `Client` instance.

### `ConnectOptions`

| Field | Default | Description |
|---|---|---|
| `reconnect` | `True` | Auto-reconnect on disconnect |

### `client.search(filters=None, limit=20) -> list[BrowserOption]`

Search for available browsers. Filters: `geo`, `language`, etc.

### `client.rent(browser_id) -> Browser`

Rent a browser by browser ID. Waits up to 60s for a match.

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

## Session profile (cookies + storage)

`browser.profile` lets you snapshot and restore cookies, `localStorage`, and `sessionStorage` between sessions — without involving the relay or backend. The blob stays in your own storage.

```python
import json

# First session — sign up, then export profile
async with await client.rent(browser_id) as browser:
    await browser.send({"method": "Page.navigate", "params": {"url": "https://reddit.com/login"}})
    # ... perform signup, 2FA ...
    profile = await browser.profile.export(domains=[".reddit.com", "reddit.com"])

with open("reddit_profile.json", "w") as f:
    json.dump(profile, f)

# Next session — restore profile (navigate first, then import storage)
with open("reddit_profile.json") as f:
    profile = json.load(f)

async with await client.rent(browser_id) as browser:
    # Cookies are domain-scoped — set them before navigation
    await browser.profile.import_(profile)
    await browser.send({"method": "Page.navigate", "params": {"url": "https://reddit.com"}})
    # already logged in
```

**Notes:**
- `localStorage`/`sessionStorage` require a document context — navigate to the target origin before calling `import_()`, or call it right after navigation.
- Cookies (`Network.setCookies`) work before any navigation.
- Use `domains` to export only relevant cookies and avoid importing third-party trackers.
- Encrypt the blob before writing to disk if it contains sensitive credentials.
- `import_()` raises `ValueError` on `schema_version` mismatch (future-proofing).

## CDP Lifecycle

The relay maintains the CDP connection to the incognito browser tab. If the connection drops, it automatically reattaches with 1s/2s/4s exponential backoff. Commands during reattach are buffered (FIFO, max 50). If 3 reattach attempts fail, a new fallback tab is created. If that also fails, `cdp_unrecoverable` error is sent.

## Real-signup examples

See `examples/SMOKE.md` for full runbook.

Quick:
```bash
pip install -e ".[dev]"
export CEKI_API_KEY=...
export BROWSER_ID=...
python examples/reddit_signup.py
```

These are NOT automated tests — they require a live relay, an online provider, and a real IMAP mailbox. Run manually as part of Phase 2 acceptance.

## Human Mode

Browser actions can optionally include human-like timing — delays before/after actions and per-character typing with jitter.

```python
# Default: natural profile (enabled by default)
browser = await client.rent(browser_id)

# Explicit profile
browser = await client.rent(browser_id, human="careful")

# Disable humanization
browser = await client.rent(browser_id, human=None)

# Custom profile dict
browser = await client.rent(browser_id, human={"typing": {"wpm": 130}})
```

### High-level methods

```python
await browser.navigate("https://example.com")
await browser.click(100, 200)
await browser.type("Hello, world!")  # Per-char with jitter when human mode on
await browser.scroll(delta_y=-300)
img_bytes = await browser.screenshot()
```

### Runtime control

```python
prev = browser.set_human("careful")  # Switch profile, returns previous
browser.set_human(None)               # Disable mid-session
```

### Environment variables

- `CEKI_HUMAN_PROFILE` — Override default profile name (e.g., `careful`)
- `CEKI_HUMAN_PROFILE_PATH` — Path to custom JSON profile file
- `CEKI_HUMAN_DISABLE=1` — Disable humanization entirely

## CLI

The SDK installs a `ceki` CLI binary on your PATH.

### Install

```bash
pip install ceki-sdk
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `CEKI_API_KEY` | yes | Agent token (`ag_...`) |

### Quick start

```bash
export CEKI_API_KEY=ag_...

BROWSER=$(ceki search --limit 1 | jq -r '.[0].browser_id')
SID=$(ceki rent --browser $BROWSER | jq -r .session_id)
ceki navigate $SID https://example.com
ceki snapshot $SID -o snap.png
ceki stop $SID
```

The CLI persists session state locally — after `rent` it saves the session ID so subsequent commands resume it by SID without re-renting.

### Commands

#### Discovery and lifecycle

| Command | Description |
|---|---|
| `search [--limit N] [--filter K=V]…` | List available browsers |
| `my-browsers` | List browsers with pre-arranged rent contracts |
| `rent --browser ID [--mode incognito\|main] [--fingerprint-from FILE]` | Rent a browser |
| `sessions [--all] [--limit N] [--json]` | List your sessions |
| `stop SID` | End a session |
| `wait SID` | Block until the session ends |

#### Browser control

| Command | Description |
|---|---|
| `navigate SID URL` | Open URL |
| `click SID X Y` | Click at viewport coordinates |
| `type SID TEXT [--natural]` | Type text into focused element |
| `scroll SID X Y DY` | Scroll from (X, Y) by `DY` pixels |
| `screenshot SID -o FILE [--format png\|jpeg] [--full]` | Save screenshot |
| `snapshot SID -o FILE` | Screenshot + new chat messages |
| `switch-tab SID` | Switch active tab |
| `upload SID --selector CSS --file PATH [--filename NAME]` | Attach file to `<input type="file">` |

#### Chat with host

| Command | Description |
|---|---|
| `chat SID send TEXT` | Send message to host |
| `chat SID next [--timeout SEC]` | Wait for next host message |
| `chat SID history [--since TS] [--limit N]` | Fetch chat history |
| `chat SID send-image --image PATH [--text MSG]` | Send image to host |

#### Advanced

| Command | Description |
|---|---|
| `profile SID export -o FILE [--domains CSV] [--no-session-storage]` | Export cookies / localStorage |
| `profile SID import -i FILE` | Import previously exported profile |
| `request-captcha SID [--acceptance SEC] [--completion SEC] [--manual]` | Ask host to solve CAPTCHA |
| `configure SID [--masking-mode VAL] [--fingerprint VAL]` | Toggle masking / fingerprint |
| `cdp SID --method METHOD [--params JSON]` | Raw CDP command |

### Output and errors

Successful commands write a single JSON line to stdout. Errors go to stderr as `{"error": "...", "code": "..."}`. Pipe stdout through `jq` to chain commands.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | generic error |
| `2` | `CEKI_API_KEY` not set |
| `3` | session not found or not owner |
| `4` | timeout |
| `5` | network / connection error |
| `130` | interrupted (Ctrl-C) |

Full reference (with EN+RU): https://browser.ceki.me/docs#cli

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check ceki_sdk/
mypy ceki_sdk/
```

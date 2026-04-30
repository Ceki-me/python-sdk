# ceki-browser

> Real browsers of real people. 5-line API. Secure P2P via WebRTC.

Python SDK for [browser.ceki.me](https://browser.ceki.me) — rent real browsers from real people for AI agent automation.

Browser commands travel over a direct WebRTC DataChannel between your agent and the provider's browser. Chat messages are routed through the relay server. The relay handles signaling, matchmaking, and chat. Connections are authenticated via STUN/TURN with identity validation.

## Installation

```bash
pip install ceki-browser
```

## Quickstart

```python
import asyncio
from ceki_browser import Browser

async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        async with await br.session(mode="incognito", domain_hints=["example.com"]) as s:
            await s.navigate("https://example.com")
            title = await s.query("h1")
            print(title.text)

asyncio.run(main())
```

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `token` | — | Sanctum API token from your [dashboard](https://browser.ceki.me/dashboard) |
| `relay_url` | `wss://browser.ceki.me/ws/agent` | WebSocket relay endpoint |

### Session options

| Parameter | Default | Description |
|---|---|---|
| `mode` | `"incognito"` | `"incognito"` (clean browser) or `"persona"` (real user cookies) |
| `domain_hints` | `[]` | Preferred domains for provider matching |
| `geo` | `""` | Preferred provider geo (e.g. `"US"`, `"DE"`) |
| `language` | `""` | Preferred browser language |
| `max_price_per_min` | `1.0` | Maximum price you're willing to pay per minute (USD) |
| `estimated_duration_min` | `30` | Estimated session duration for provider matching |

## Methods

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `navigate(url)` | `url`, `timeout_ms=120000` | `NavigateResult` | Navigate to URL |
| `query(selector)` | `selector`, `attributes=["textContent"]` | `QueryResult` | Query first matching element |
| `query_all(selector)` | `selector`, `attributes`, `limit=20` | `QueryResult` | Query all matching elements |
| `get_html(selector)` | `selector="html"`, `outer=True` | `HtmlResult` | Get element HTML |
| `click(selector)` | `selector` or `x`/`y` coordinates | — | Click element or coordinates |
| `type(selector, text)` | `selector`, `text`, `delay_ms=0` | — | Type text into input |
| `scroll(selector)` | `selector` or `direction`/`amount` | — | Scroll to element or direction |
| `screenshot()` | `format="png"`, `quality=80` | `ScreenshotResult` | Capture visible tab |
| `back()` / `forward()` / `reload()` | — | `NavigateResult` | Navigation controls |
| `inject_credentials(secret_id, target)` | `secret_id`, `target` selectors | `dict` | Fill credentials from vault |
| `request_human_action(type, message)` | `action_type`, `message`, `timeout_sec=120` | `HumanActionResult` | Ask browser owner for help |

### Credential Vault

`inject_credentials` fills login forms using encrypted secrets stored on the provider side.
The SDK sends a `secret_id` — the provider extension decrypts and injects credentials locally (RSA-OAEP + AES-256-GCM).

Create secrets via dashboard: **API Keys & Secrets** section.

## Errors

| Error | When |
|---|---|
| `AuthError` | Invalid or expired token |
| `ProviderDisconnected` | Provider went offline during session |
| `NavigationTimeout` | `navigate()` exceeded timeout |
| `CommandTimeout` | Any command exceeded timeout |
| `RateLimited` | Too many sessions/commands per hour |
| `ProviderNotVerified` | `inject_credentials` requires a verified provider |
| `HumanActionDeclined` | Browser owner declined the action |
| `HumanActionTimeout` | Browser owner didn't respond in time |

## Chat

During a browser session, your agent can exchange messages and images with the browser provider via `session.chat`. Chat is routed through the relay server.

```python
from ceki_browser import Browser

async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        session = await br.session(mode="incognito", domain_hints=["example.com"])

        # Listen for incoming text messages
        session.chat.on_message(lambda msg: print(f"Provider: {msg.content}"))

        # Send text
        await session.chat.send("Starting automation, please don't close the browser")

        # Send image (bytes or path)
        await session.chat.send_image(b"\x89PNG...", "image/png")
        await session.chat.send_image("screenshot.png")

        # Fetch message history from server
        messages = await session.chat.history()
        for msg in messages:
            print(msg)

        await session.end()
```

### Direct Chat (chat-service REST + WS)

For server-side chat access (polling, recovery, live push) independent of P2P:

```python
async with Browser(token=TOKEN) as br:
    session = await br.session()

    # topic_id from rent or passed manually
    chat = session.chat_direct(topic_id="<topic_id>")

    # Fetch message history (forward cursor)
    msgs = await chat.history(after="<last_known_id>", limit=50)

    # Send a message via REST
    await chat.send("Hello from agent")

    # Subscribe to live push via WebSocket
    async def on_msg(msg):
        print("new:", msg.get("content"))

    await chat.subscribe(on_msg)

    # ... do work ...
    await chat.close()
```

Set `CEKI_CHAT_SERVICE_URL` env var to override the chat-service URL (default: `https://chat.ceki.me`).

## Human Mode

SDK includes built-in human-like behavior simulation (delays, typing jitter) enabled by default.

### Profiles

```python
# Default — natural delays (enabled by default)
async with Browser(token, human="natural") as br:
    s = await br.session()

# Careful — slower, more human-like
async with Browser(token, human="careful") as br:
    s = await br.session()

# Disabled — no delays
async with Browser(token, human=None) as br:
    s = await br.session()

# Custom profile from dict
async with Browser(token, human={"typing": {"wpm": 140}, "pre_action_ms": {"click": [50, 200]}}) as br:
    s = await br.session()

# Custom profile from JSON file
async with Browser(token, human="./my_profile.json") as br:
    s = await br.session()
```

### Runtime Profile Change

```python
prev = s.set_human("careful")  # switch to careful
await s.type("#email", "user@example.com")
s.set_human(prev)  # restore previous
```

### Profile JSON Schema

```json
{
  "version": 1,
  "name": "natural",
  "typing": {
    "wpm": 110,
    "jitter": 0.35,
    "thinking_pause_prob": 0.012,
    "thinking_pause_ms": [300, 1200],
    "typo_prob": 0.0
  },
  "pre_action_ms": {
    "click": [80, 350],
    "type": [120, 500],
    "scroll": [50, 250],
    "navigate": [0, 0],
    "screenshot": [0, 0]
  },
  "post_action_ms": {
    "click": [150, 800],
    "type": [150, 800],
    "scroll": [200, 900],
    "navigate": [400, 1800],
    "screenshot": [0, 0]
  },
  "mouse": {
    "move_before_click": false,
    "trajectory": "off"
  },
  "rng_seed": null
}
```

### Environment Variables

| Variable | Description |
|---|---|
| `CEKI_HUMAN_PROFILE` | Preset name (`natural`, `careful`) |
| `CEKI_HUMAN_PROFILE_PATH` | Path to custom JSON profile |
| `CEKI_HUMAN_DISABLE=1` | Disable all human-mode delays |

Priority: explicit `Browser(human=...)` > env vars > default (`natural`).

## Examples

- [`quickstart.py`](examples/quickstart.py) — minimal 5-line example
- [`scraping.py`](examples/scraping.py) — query DOM elements
- [`login_flow.py`](examples/login_flow.py) — inject credentials + 2FA

## Pricing

See [browser.ceki.me/pricing](https://browser.ceki.me/pricing).

## License

MIT

# ceki-browser

> Real browsers of real people. 5-line API. Secure P2P via WebRTC.

Python SDK for [browser.ceki.me](https://browser.ceki.me) — rent real browsers from real people for AI agent automation.

All browser commands and chat messages travel over a direct WebRTC DataChannel between your agent and the provider's browser — the relay server only handles signaling and matchmaking. Connections are authenticated via STUN/TURN with identity validation.

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

During a browser session, your agent can exchange messages and images with the browser provider via `session.chat`. Chat flows over a dedicated `ceki-chat` WebRTC DataChannel — messages are ephemeral (in-memory only, not persisted).

```python
from ceki_browser import Browser

async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        session = await br.session(mode="incognito", domain_hints=["example.com"])

        # Listen for incoming text messages
        session.chat.on_message(lambda msg: print(f"Provider: {msg.text}"))

        # Listen for incoming images
        session.chat.on_image(lambda img: print(f"Image received: {img.mime}, {len(img.data)} bytes"))

        # Send text
        await session.chat.send("Starting automation, please don't close the browser")

        # Send image (bytes, path, or base64)
        await session.chat.send_image(b"\x89PNG...", "image/png")
        await session.chat.send_image("screenshot.png")

        # Access ephemeral history
        for item in session.chat.history:
            print(item)

        await session.end()
```

Images over 5MB are auto-downscaled (requires Pillow) or rejected. Large images are chunked over the DataChannel (12KB chunks, 30s assembly timeout).

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

## Examples

- [`quickstart.py`](examples/quickstart.py) — minimal 5-line example
- [`scraping.py`](examples/scraping.py) — query DOM elements
- [`login_flow.py`](examples/login_flow.py) — inject credentials + 2FA

## Pricing

See [browser.ceki.me/pricing](https://browser.ceki.me/pricing).

## License

MIT

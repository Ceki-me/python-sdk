# ceki-browser

> Real browsers of real people. 5-line API.

Python SDK for [browser.ceki.me](https://browser.ceki.me) — rent real browsers from real people for AI agent automation.

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

During a browser session, your agent can exchange messages with the browser provider via `session.chat`. The chat channel opens automatically when the session starts.

```python
from ceki_browser import Browser

async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        session = await br.session(mode="incognito", domain_hints=["example.com"])

        # Listen for incoming messages from the provider
        def on_provider_message(msg):
            print(f"Provider says: {msg.content}")
            if "captcha" in msg.content.lower():
                # Take a screenshot and send it to the provider
                asyncio.create_task(handle_captcha(session))

        session.chat.on_message(on_provider_message)

        # Send a message
        await session.chat.send("Starting automation, please don't close the browser")

        # Send a screenshot
        await session.chat.send_image(Path("screenshot.png"))

        # Fetch message history
        history = await session.chat.history(limit=20)

        await session.close()
```

## Examples

- [`quickstart.py`](examples/quickstart.py) — minimal 5-line example
- [`scraping.py`](examples/scraping.py) — query DOM elements
- [`login_flow.py`](examples/login_flow.py) — inject credentials + 2FA

## Pricing

See [browser.ceki.me/pricing](https://browser.ceki.me/pricing).

## License

MIT

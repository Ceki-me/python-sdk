"""
Reddit signup via ceki-browser SDK.

Run:
  CEKI_API_KEY=... \\
  CEKI_RELAY_URL=wss://relay.ittribe.org/ws/agent \\
  SCHEDULE_ID=42 \\
  IMAP_HOST=mail.ceki.me IMAP_USER=kom@ceki.me IMAP_PASS=... \\
  EMAIL_TAG=browserlend1 \\
  python examples/reddit_signup.py

Requires: provider with schedule_id=42 online and accepting rents.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import string

from ceki_browser import connect

from .imap_helper import wait_for_confirm_link


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def main() -> None:
    api_key = os.environ["CEKI_API_KEY"]
    relay_url = os.environ.get("CEKI_RELAY_URL", "wss://relay.ittribe.org/ws/agent")
    schedule_id = int(os.environ["SCHEDULE_ID"])
    email_tag = os.environ.get("EMAIL_TAG", f"browserlend-{secrets.token_hex(4)}")

    email_addr = f"kom+{email_tag}@ceki.me"
    username = f"tribe_{secrets.token_hex(4)}"
    password = _random_password()

    print(f"[reddit_signup] email={email_addr} username={username}")

    client = await connect(api_key, relay_url=relay_url)
    browser = await client.rent(schedule_id)
    print(f"[session] id={browser.session_id} chat_topic_id={browser.chat_topic_id}")
    print(f"[session] browser_info={browser.browser_info}")

    provider_replies: asyncio.Queue[str] = asyncio.Queue()

    async def on_chat(msg) -> None:
        if msg.is_system():
            return
        if msg.is_from_provider(browser.provider_user_id) and msg.text:
            await provider_replies.put(msg.text)

    browser.chat.on_message(on_chat)

    async def on_tab(url: str) -> None:
        print(f"[tab_opened] {url} — switching")
        await browser.switch_tab()

    browser.on_tab_opened(on_tab)

    load_fired = asyncio.Event()
    frame_navigated = asyncio.Event()

    async def on_event(method: str, params: dict) -> None:
        if method == "Page.loadEventFired":
            load_fired.set()
        elif method == "Page.frameNavigated":
            frame_navigated.set()

    browser.on_event(on_event)

    await browser.send({"method": "Page.enable"})
    await browser.send({"method": "Network.enable"})

    load_fired.clear()
    await browser.send({
        "method": "Page.navigate",
        "params": {"url": "https://www.reddit.com/register"},
    })
    await asyncio.wait_for(load_fired.wait(), timeout=30)
    print("[nav] register page loaded")

    async def fill_field(selector: str, value: str) -> None:
        await browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": f"""
                    (function() {{
                        var el = document.querySelector({repr(selector)});
                        if (!el) return false;
                        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(el, {repr(value)});
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }})()
                """
            },
        })

    await asyncio.sleep(1)
    await fill_field('input[name="email"]', email_addr)
    await asyncio.sleep(0.3)
    await fill_field('input[name="username"]', username)
    await asyncio.sleep(0.3)
    await fill_field('input[name="password"]', password)
    await asyncio.sleep(0.5)

    await browser.send({
        "method": "Runtime.evaluate",
        "params": {
            "expression": """
                (function() {
                    var btn = document.querySelector('button[type="submit"]');
                    if (btn) { btn.click(); return true; }
                    return false;
                })()
            """
        },
    })
    print("[form] submitted")

    await asyncio.sleep(3)

    captcha_detected = await browser.send({
        "method": "Runtime.evaluate",
        "params": {
            "expression": """
                !!(document.querySelector('iframe[src*="captcha"]') ||
                   document.querySelector('[data-testid="captcha"]') ||
                   document.title.toLowerCase().includes('captcha'))
            """
        },
    })

    if captcha_detected.get("result", {}).get("value"):
        print("[captcha] detected — sending screenshot to provider")
        shot = await browser.send({"method": "Page.captureScreenshot"})
        png = base64.b64decode(shot["data"])
        await browser.chat.send_image(png)
        await browser.chat.send(
            "Please solve the captcha visible on screen and reply with the answer text"
        )
        print("[captcha] waiting for provider answer (up to 300s)...")
        answer = await asyncio.wait_for(provider_replies.get(), timeout=300)
        print(f"[captcha] provider answered: {answer!r}")

        await browser.send({
            "method": "Input.insertText",
            "params": {"text": answer},
        })
        await asyncio.sleep(1)
        await browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": """
                    (function() {
                        var btn = document.querySelector('button[type="submit"]');
                        if (btn) { btn.click(); return true; }
                        return false;
                    })()
                """
            },
        })

    print(f"[imap] waiting for confirm email to {email_addr}...")
    confirm_url = await wait_for_confirm_link(email_tag, timeout=120, service="reddit")
    print(f"[imap] got confirm link: {confirm_url}")

    load_fired.clear()
    await browser.send({"method": "Page.navigate", "params": {"url": confirm_url}})
    await asyncio.wait_for(load_fired.wait(), timeout=30)

    title_res = await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": "document.title"},
    })
    print(f"[confirm] page title: {title_res.get('result', {}).get('value', '')}")
    print(f"✅ Reddit account created: {username} / {email_addr}")

    await browser.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

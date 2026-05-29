"""
GitHub signup via ceki-sdk SDK.

Run:
  CEKI_API_KEY=... \\
  CEKI_RELAY_URL=wss://relay.ittribe.org/ws/agent \\
  IMAP_HOST=mail.ceki.me IMAP_USER=kom@ceki.me IMAP_PASS=... \\
  EMAIL_TAG=browserlend2 \\
  python examples/github_signup.py

Discovers an online provider via `client.search()` and rents the first one.
Optional `BROWSER_ID=N` env pins a specific provider (skip discovery).
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import string

from ceki_sdk import connect
from ceki_sdk._connect import ConnectOptions

from .imap_helper import wait_for_confirm_link


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def main() -> None:
    api_key = os.environ["CEKI_API_KEY"]
    relay_url = os.environ.get("CEKI_RELAY_URL", "wss://relay.ittribe.org/ws/agent")
    pinned_browser_id = os.environ.get("BROWSER_ID")
    email_tag = os.environ.get("EMAIL_TAG", f"browserlend-{secrets.token_hex(4)}")
    email_base = os.environ.get("EMAIL_BASE", "kom@ceki.me")
    local, _, domain = email_base.partition("@")

    email_addr = f"{local}+{email_tag}@{domain}"
    username = f"tribe-{secrets.token_hex(4)}"
    password = _random_password()

    print(f"[github_signup] email={email_addr} username={username}")

    client = await connect(api_key, ConnectOptions(relay_url=relay_url))

    if pinned_browser_id is not None:
        browser_id = int(pinned_browser_id)
        print(f"[search] using pinned BROWSER_ID={browser_id}")
    else:
        options = await client.search({})
        if not options:
            print("[search] no online providers — try later")
            await client.close()
            return
        browser_id = options[0].browser_id
        print(f"[search] found {len(options)} provider(s), renting browser_id={browser_id}")

    browser = await client.rent(browser_id)
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

    async def on_event(method: str, params: dict) -> None:
        if method == "Page.loadEventFired":
            load_fired.set()

    browser.on_event(on_event)

    load_fired.clear()
    await browser.send({"method": "Page.navigate", "params": {"url": "https://github.com/signup"}})
    await asyncio.wait_for(load_fired.wait(), timeout=30)
    print("[nav] signup page loaded")

    async def type_into(selector: str, value: str) -> None:
        await browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": f"""
                    (function() {{
                        var el = document.querySelector({repr(selector)});
                        if (!el) return false;
                        el.focus();
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
    await type_into("#email", email_addr)
    await asyncio.sleep(0.5)

    await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": "document.querySelector('button[type=submit]')?.click()"},
    })
    await asyncio.sleep(1)

    await type_into("#password", password)
    await asyncio.sleep(0.3)

    await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": "document.querySelector('button[type=submit]')?.click()"},
    })
    await asyncio.sleep(1)

    await type_into("#login", username)
    await asyncio.sleep(0.3)

    await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": "document.querySelector('button[type=submit]')?.click()"},
    })
    await asyncio.sleep(2)

    max_captcha_rounds = 3
    for round_num in range(1, max_captcha_rounds + 1):
        captcha_res = await browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": """
                    !!(document.querySelector('[data-hcaptcha-widget-id]') ||
                       document.querySelector('.captcha-container') ||
                       document.querySelector('iframe[title*="captcha" i]'))
                """
            },
        })
        if not captcha_res.get("result", {}).get("value"):
            print(f"[captcha] round {round_num}: no captcha detected, proceeding")
            break

        print(f"[captcha] round {round_num}: detected — sending screenshot to provider")
        shot = await browser.send({"method": "Page.captureScreenshot"})
        png = base64.b64decode(shot["data"])
        await browser.chat.send_image(png)
        await browser.chat.send(
            f"GitHub captcha puzzle (round {round_num}). "
            "Please solve it and reply with the answer or describe what to click."
        )
        print(f"[captcha] round {round_num}: waiting for provider (up to 300s)...")
        answer = await asyncio.wait_for(provider_replies.get(), timeout=300)
        print(f"[captcha] round {round_num}: provider answered: {answer!r}")

        await browser.send({
            "method": "Input.insertText",
            "params": {"text": answer},
        })
        await asyncio.sleep(1)
        await browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": "document.querySelector('button[type=submit]')?.click()"
            },
        })
        await asyncio.sleep(2)

    print(f"[imap] waiting for confirm email to {email_addr}...")
    confirm_url = await wait_for_confirm_link(email_tag, timeout=120, service="github")
    print(f"[imap] got confirm link: {confirm_url}")

    load_fired.clear()
    await browser.send({"method": "Page.navigate", "params": {"url": confirm_url}})
    await asyncio.wait_for(load_fired.wait(), timeout=30)

    title_res = await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": "document.title"},
    })
    print(f"[confirm] page title: {title_res.get('result', {}).get('value', '')}")
    print(f"✅ GitHub account created: {username} / {email_addr}")

    await browser.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

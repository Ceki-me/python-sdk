from __future__ import annotations

import asyncio
import base64
import os

from ceki_browser import connect


async def main() -> None:
    client = await connect(os.environ["CEKI_API_KEY"])
    browser = await client.rent(int(os.environ["SCHEDULE_ID"]))

    provider_replied = asyncio.Event()
    provider_text: dict[str, str] = {}

    async def on_msg(msg) -> None:
        if msg.is_system():
            return
        if msg.is_from_provider(browser.provider_user_id):
            provider_text["value"] = msg.text or ""
            provider_replied.set()

    browser.chat.on_message(on_msg)

    await browser.send({"method": "Page.navigate", "params": {"url": "https://reddit.com/register"}})
    shot = await browser.send({"method": "Page.captureScreenshot"})

    png = base64.b64decode(shot["data"])
    await browser.chat.send_image(png)
    await browser.chat.send("Please solve the captcha, return text only")

    await asyncio.wait_for(provider_replied.wait(), timeout=120)
    print("Provider:", provider_text["value"])

    await browser.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

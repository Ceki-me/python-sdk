from __future__ import annotations

import asyncio
import os

from ceki_browser import connect


async def main() -> None:
    client = await connect(os.environ["CEKI_API_KEY"])
    browsers = await client.search(limit=5)
    if not browsers:
        print("no browsers available")
        await client.close()
        return

    browser = await client.rent(browsers[0].schedule_id)

    load_fired = asyncio.Event()

    async def on_event(method: str, params: dict) -> None:
        if method == "Page.loadEventFired":
            load_fired.set()

    browser.on_event(on_event)

    await browser.send({"method": "Page.enable"})
    await browser.send({"method": "Page.navigate", "params": {"url": "https://example.com"}})

    await asyncio.wait_for(load_fired.wait(), timeout=30)
    print("page loaded")

    shot = await browser.send({"method": "Page.captureScreenshot"})
    print(f"screenshot data length: {len(shot.get('data', ''))}")

    await browser.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

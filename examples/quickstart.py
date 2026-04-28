"""Quickstart — 5 lines to a real browser."""
import asyncio

from ceki_browser import Browser


async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        async with await br.session(mode="incognito", domain_hints=["example.com"]) as s:
            await s.navigate("https://example.com")
            title = await s.query("h1")
            print(title.text)


if __name__ == "__main__":
    asyncio.run(main())

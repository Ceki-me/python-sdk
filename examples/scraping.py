"""Scraping example — query multiple elements."""
import asyncio

from ceki_sdk import Browser


async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        async with await br.session(mode="incognito") as s:
            await s.navigate("https://news.ycombinator.com")

            items = await s.query_all("a.titlelink", attributes=["textContent", "href"], limit=10)
            for el in items.elements:
                print(f"{el.get('textContent')} — {el.get('href')}")

            html = await s.get_html("body", outer=False)
            print(f"\nBody HTML length: {len(html.html)}")


if __name__ == "__main__":
    asyncio.run(main())

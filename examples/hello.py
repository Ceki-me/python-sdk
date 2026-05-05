import asyncio
import os

from ceki_browser import connect


async def main() -> None:
    api_key = os.environ["CEKI_API_KEY"]
    client = await connect(api_key)
    print("Connected to relay")

    options = await client.search({"geo": "US"}, limit=5)
    print(f"Found {len(options)} browser(s)")
    for opt in options:
        print(f"  schedule_id={opt.schedule_id} geo={opt.geo} price={opt.price_per_min}/min")

    await client.close()
    print("Done")


if __name__ == "__main__":
    asyncio.run(main())

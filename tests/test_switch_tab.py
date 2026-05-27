from __future__ import annotations

import asyncio

import pytest

from ceki_sdk import ConnectOptions, connect


@pytest.fixture
async def browser_fixture(mock_relay):
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))

    async def ack_rent():
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": "ev-tab", "schedule_id": 1})
        await asyncio.sleep(0.02)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": "ev-tab",
            "session_id": "sess-tab",
            "schedule_id": 1,
            "chat_topic_id": None,
            "browser_info": {},
        })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t
    yield browser, mock_relay
    await client.close()


@pytest.mark.asyncio
async def test_tab_opened_callback(browser_fixture):
    browser, mock_relay = browser_fixture

    opened_urls: list[str] = []

    async def on_tab(url: str) -> None:
        opened_urls.append(url)

    browser.on_tab_opened(on_tab)

    await mock_relay.send_to_all({
        "type": "tab_opened",
        "session_id": "sess-tab",
        "url": "https://popup.example.com",
    })

    await asyncio.sleep(0.1)

    assert opened_urls == ["https://popup.example.com"]


@pytest.mark.asyncio
async def test_switch_tab_sends_correct_msg(browser_fixture):
    browser, mock_relay = browser_fixture

    await browser.switch_tab()
    await asyncio.sleep(0.05)

    switch_msgs = [m for m in mock_relay.received if m.get("type") == "switch_tab"]
    assert len(switch_msgs) == 1
    assert switch_msgs[0]["session_id"] == "sess-tab"

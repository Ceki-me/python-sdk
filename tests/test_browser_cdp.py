from __future__ import annotations

import asyncio

import pytest

from ceki_browser import connect


@pytest.fixture
async def connected_client(mock_relay):
    client = await connect("test-key", relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent")
    yield client, mock_relay
    await client.close()


async def _do_rent(client, mock_relay, session_id="sess-1", schedule_id=42):
    async def ack_rent():
        await asyncio.sleep(0.05)
        for msg in mock_relay.received:
            if msg.get("type") == "rent":
                event_id = msg["event_id"]
                break
        else:
            event_id = "unknown"
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": event_id,
            "session_id": session_id,
            "schedule_id": schedule_id,
            "chat_topic_id": None,
            "browser_info": {},
        })

    task = asyncio.create_task(ack_rent())
    browser = await client.rent(schedule_id)
    await task
    return browser


@pytest.mark.asyncio
async def test_cdp_happy_path(connected_client):
    client, mock_relay = connected_client
    browser = await _do_rent(client, mock_relay)

    async def send_cdp_response():
        await asyncio.sleep(0.05)
        cdp_msg = next((m for m in mock_relay.received if m.get("type") == "cdp"), None)
        assert cdp_msg is not None
        await mock_relay.send_to_all({
            "type": "cdp_response",
            "session_id": browser.session_id,
            "id": cdp_msg["id"],
            "ok": True,
            "result": {"frameId": "abc123"},
        })

    task = asyncio.create_task(send_cdp_response())
    result = await browser.send({"method": "Page.navigate", "params": {"url": "https://example.com"}})
    await task

    assert result["frameId"] == "abc123"

    sent = [m for m in mock_relay.received if m.get("type") == "cdp"]
    assert sent[-1]["method"] == "Page.navigate"
    assert sent[-1]["params"]["url"] == "https://example.com"
    assert sent[-1]["session_id"] == browser.session_id


@pytest.mark.asyncio
async def test_cdp_on_event_callback(connected_client):
    client, mock_relay = connected_client
    browser = await _do_rent(client, mock_relay)

    received_events: list[tuple[str, dict]] = []

    async def on_ev(method: str, params: dict) -> None:
        received_events.append((method, params))

    browser.on_event(on_ev)

    await mock_relay.send_to_all({
        "type": "cdp_event",
        "session_id": browser.session_id,
        "method": "Page.loadEventFired",
        "params": {"timestamp": 1.23},
    })

    await asyncio.sleep(0.1)

    assert len(received_events) == 1
    assert received_events[0] == ("Page.loadEventFired", {"timestamp": 1.23})


@pytest.mark.asyncio
async def test_cdp_timeout(connected_client):
    client, mock_relay = connected_client
    browser = await _do_rent(client, mock_relay)

    cdp = {"method": "Page.navigate", "params": {"url": "https://example.com"}}
    with pytest.raises(asyncio.TimeoutError):
        await browser.send(cdp, timeout=0.05)


@pytest.mark.asyncio
async def test_cdp_error_response(connected_client):
    client, mock_relay = connected_client
    browser = await _do_rent(client, mock_relay)

    async def send_error():
        await asyncio.sleep(0.05)
        cdp_msg = next((m for m in mock_relay.received if m.get("type") == "cdp"), None)
        await mock_relay.send_to_all({
            "type": "cdp_response",
            "session_id": browser.session_id,
            "id": cdp_msg["id"],
            "ok": False,
            "error": {"message": "No such target"},
        })

    task = asyncio.create_task(send_error())
    with pytest.raises(Exception, match="CDP error"):
        await browser.send({"method": "Page.navigate", "params": {"url": "https://example.com"}})
    await task

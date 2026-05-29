from __future__ import annotations

import asyncio

import pytest

from ceki_sdk import ConnectOptions, connect
from ceki_sdk._exceptions import (
    CdpUnrecoverable,
    InsufficientFunds,
    RateLimitExceeded,
    SessionEnded,
)


@pytest.fixture
async def browser_and_relay(mock_relay):
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))

    async def ack_rent():
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": "ev-1", "browser_id": 1})
        await asyncio.sleep(0.02)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": "ev-1",
            "session_id": "sess-err",
            "browser_id": 1,
            "chat_topic_id": None,
            "browser_info": {},
        })

    task = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await task
    yield browser, mock_relay
    await client.close()


@pytest.mark.asyncio
async def test_error_1011_heartbeat_timeout(browser_and_relay):
    browser, mock_relay = browser_and_relay

    cdp = {"method": "Page.navigate", "params": {"url": "https://x.com"}}

    async def pending_cdp():
        with pytest.raises(SessionEnded):
            await browser.send(cdp, timeout=5)

    task = asyncio.create_task(pending_cdp())
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({
        "type": "error",
        "session_id": "sess-err",
        "code": -1011,
        "message": "heartbeat_timeout",
    })

    reason = await asyncio.wait_for(browser.wait_until_ended(), timeout=2)
    assert reason == "heartbeat_timeout"
    await task


@pytest.mark.asyncio
async def test_error_1012_insufficient_funds(browser_and_relay):
    browser, mock_relay = browser_and_relay
    cdp = {"method": "Page.navigate", "params": {"url": "https://x.com"}}

    async def pending_cdp():
        with pytest.raises(InsufficientFunds):
            await browser.send(cdp, timeout=5)

    task = asyncio.create_task(pending_cdp())
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({
        "type": "error",
        "session_id": "sess-err",
        "code": -1012,
        "message": "insufficient_funds",
    })

    reason = await asyncio.wait_for(browser.wait_until_ended(), timeout=2)
    assert reason == "insufficient_funds"
    await task


@pytest.mark.asyncio
async def test_error_1013_rate_limit_does_not_end_session(browser_and_relay):
    browser, mock_relay = browser_and_relay

    async def pending_cdp():
        await asyncio.sleep(0.05)
        cdp_msg = next((m for m in mock_relay.received if m.get("type") == "cdp"), None)
        assert cdp_msg is not None
        await mock_relay.send_to_all({
            "type": "error",
            "session_id": "sess-err",
            "code": -1013,
            "id": cdp_msg["id"],
            "retry_after": 2.5,
        })

    cdp = {"method": "Page.navigate", "params": {"url": "https://x.com"}}
    task = asyncio.create_task(pending_cdp())
    with pytest.raises(RateLimitExceeded) as exc_info:
        await browser.send(cdp, timeout=5)
    await task

    assert exc_info.value.retry_after == 2.5
    assert not browser._ended.is_set()


@pytest.mark.asyncio
async def test_error_1050_cdp_unrecoverable_does_not_end_session(browser_and_relay):
    browser, mock_relay = browser_and_relay

    async def pending_cdp():
        await asyncio.sleep(0.05)
        cdp_msg = next((m for m in mock_relay.received if m.get("type") == "cdp"), None)
        assert cdp_msg is not None
        await mock_relay.send_to_all({
            "type": "error",
            "session_id": "sess-err",
            "code": -1050,
            "id": cdp_msg["id"],
            "message": "CDP pipe broken",
        })

    cdp = {"method": "Page.navigate", "params": {"url": "https://x.com"}}
    task = asyncio.create_task(pending_cdp())
    with pytest.raises(CdpUnrecoverable) as exc_info:
        await browser.send(cdp, timeout=5)
    await task

    assert "CDP pipe broken" in str(exc_info.value)
    assert not browser._ended.is_set()

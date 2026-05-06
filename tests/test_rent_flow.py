from __future__ import annotations

import asyncio

import pytest

from ceki_browser import Client, connect
from ceki_browser._exceptions import (
    RateLimitExceeded,
    SessionEnded,
)

from .conftest import MockRelayServer


@pytest.mark.asyncio
async def test_rent_resolves_via_rent_pending_then_match(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", relay_url=url)

    rent_task = asyncio.create_task(client.rent(schedule_id=240))
    await asyncio.sleep(0.05)

    # Relay sends rent_pending with server-assigned event_id
    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "1924", "schedule_id": 240})
    await asyncio.sleep(0.05)

    # Relay sends match with same event_id
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "1924",
        "session_id": "1924",
        "schedule_id": 240,
        "capabilities": {},
        "price_per_min": 0.01,
    })

    browser = await asyncio.wait_for(rent_task, timeout=5)
    assert browser.session_id == "1924"
    assert browser.schedule_id == 240

    # Verify WS rent message had only type + schedule_id (no event_id, no duration_minutes)
    rent_msgs = [m for m in mock_relay.received if m.get("type") == "rent"]
    assert len(rent_msgs) == 1
    assert set(rent_msgs[0].keys()) == {"type", "schedule_id"}
    assert rent_msgs[0]["schedule_id"] == 240

    await client.close()


@pytest.mark.asyncio
async def test_rent_error_with_event_id_raises_exception(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", relay_url=url)

    rent_task = asyncio.create_task(client.rent(schedule_id=240))
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "777", "schedule_id": 240})
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({
        "type": "error",
        "code": -1015,
        "reason": "no_providers",
        "event_id": "777",
    })

    with pytest.raises(Exception) as exc_info:
        await asyncio.wait_for(rent_task, timeout=5)
    assert "relay error -1015" in str(exc_info.value)

    await client.close()


@pytest.mark.asyncio
async def test_rent_early_error_without_event_id_raises_exception(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", relay_url=url)

    rent_task = asyncio.create_task(client.rent(schedule_id=240))
    await asyncio.sleep(0.05)

    # Early error before rent_pending (e.g. rate limit) — no event_id
    await mock_relay.send_to_all({
        "type": "error",
        "code": -1013,
        "retry_after": 2.0,
    })

    with pytest.raises(RateLimitExceeded):
        await asyncio.wait_for(rent_task, timeout=5)

    await client.close()

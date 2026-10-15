from __future__ import annotations

import asyncio

import pytest

from ceki_sdk import Client, ConnectOptions, connect
from ceki_sdk._exceptions import (
    CekiError,
    ProviderOffline,
    RateLimitExceeded,
)
from tests.test_profile import SAMPLE_FINGERPRINT

from .conftest import MockRelayServer


@pytest.mark.asyncio
async def test_rent_resolves_via_rent_pending_then_match(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

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
    assert set(rent_msgs[0].keys()) == {"type", "browser_id"}
    assert rent_msgs[0]["browser_id"] == 240

    await client.close()


@pytest.mark.asyncio
async def test_rent_error_with_event_id_raises_exception(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

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

    with pytest.raises(ProviderOffline) as exc_info:
        await asyncio.wait_for(rent_task, timeout=5)
    assert "no_providers" in str(exc_info.value)

    await client.close()


@pytest.mark.asyncio
async def test_rent_early_error_without_event_id_raises_exception(
    mock_relay: MockRelayServer,
) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

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


@pytest.mark.asyncio
async def test_rent_main_mode_forwards_mode_in_ws_message(mock_relay: MockRelayServer) -> None:
    """Regression for task 399 / #310: rent(mode='main') must put mode in the
    WS rent payload so the relay forwards it to POST /api/sessions and the
    backend opens a main-profile session."""
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(schedule_id=240, mode="main"))
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "m1", "schedule_id": 240})
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "m1",
        "session_id": "m1",
        "schedule_id": 240,
        "capabilities": {},
        "price_per_min": 0.01,
    })
    await asyncio.wait_for(rent_task, timeout=5)

    rent_msgs = [m for m in mock_relay.received if m.get("type") == "rent"]
    assert len(rent_msgs) == 1
    assert rent_msgs[0].get("mode") == "main"
    assert rent_msgs[0]["browser_id"] == 240

    await client.close()


@pytest.mark.asyncio
async def test_rent_with_fingerprint_dict_sends_configure(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(schedule_id=240, fingerprint=SAMPLE_FINGERPRINT))
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "fp1", "schedule_id": 240})
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "fp1",
        "session_id": "fp1",
        "schedule_id": 240,
        "capabilities": {},
        "price_per_min": 0.01,
    })

    browser = await asyncio.wait_for(rent_task, timeout=5)
    assert browser.session_id == "fp1"
    await asyncio.sleep(0.1)

    configure_msgs = [m for m in mock_relay.received if m.get("type") == "session.configure"]
    assert len(configure_msgs) == 1
    assert configure_msgs[0]["fingerprint"] == SAMPLE_FINGERPRINT
    assert configure_msgs[0]["session_id"] == "fp1"

    await client.close()


@pytest.mark.asyncio
async def test_rent_with_fingerprint_false_sends_configure_false(
    mock_relay: MockRelayServer,
) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(schedule_id=240, fingerprint=False))
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "fp2", "schedule_id": 240})
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "fp2",
        "session_id": "fp2",
        "schedule_id": 240,
        "capabilities": {},
        "price_per_min": 0.01,
    })

    await asyncio.wait_for(rent_task, timeout=5)
    await asyncio.sleep(0.1)

    configure_msgs = [m for m in mock_relay.received if m.get("type") == "session.configure"]
    assert len(configure_msgs) == 1
    assert configure_msgs[0]["fingerprint"] is False

    await client.close()


@pytest.mark.asyncio
async def test_rent_with_fingerprint_true_no_configure(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(schedule_id=240, fingerprint=True))
    await asyncio.sleep(0.05)

    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "fp3", "schedule_id": 240})
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "fp3",
        "session_id": "fp3",
        "schedule_id": 240,
        "capabilities": {},
        "price_per_min": 0.01,
    })

    await asyncio.wait_for(rent_task, timeout=5)

    configure_msgs = [m for m in mock_relay.received if m.get("type") == "session.configure"]
    assert len(configure_msgs) == 0

    await client.close()


def _http_response(status: int, json_data: dict | list | None) -> object:
    import httpx
    req = httpx.Request("POST", "http://test")
    return httpx.Response(status, json=json_data, request=req)


@pytest.mark.asyncio
async def test_rent_with_subscription_resolves_browser_then_rents(
    mock_relay: MockRelayServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rent(subscription_id=...) resolves a free browser via REST, then sends a
    WS rent with browser_id + subscription_id (the relay forwards both so the
    backend re-uses the Pending session and tags rent_price=0)."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("CEKI_FORCE_WS", "1")
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    # REST resolve: POST /api/sessions {subscription_id} → {schedule_id}
    mock_post = AsyncMock(return_value=_http_response(
        201, {"session_id": 15508, "schedule_id": 42306, "data": {"rent_price": 0}}
    ))

    with patch("httpx.AsyncClient.post", mock_post):
        rent_task = asyncio.create_task(client.rent(subscription_id=709))
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all(
            {"type": "rent_pending", "event_id": "sub1", "schedule_id": 42306}
        )
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": "sub1",
            "session_id": "sub1",
            "schedule_id": 42306,
            "capabilities": {},
            "price_per_min": 0.0,
        })

        browser = await asyncio.wait_for(rent_task, timeout=5)

    assert browser.session_id == "sub1"
    # REST resolve hit the right endpoint with subscription_id
    assert mock_post.call_args[0][0] == "https://api.ceki.me/api/sessions"
    assert mock_post.call_args[1]["json"] == {"subscription_id": 709, "mode": "incognito"}
    # WS rent carries browser_id (resolved) + subscription_id
    rent_msgs = [m for m in mock_relay.received if m.get("type") == "rent"]
    assert len(rent_msgs) == 1
    assert rent_msgs[0]["browser_id"] == 42306
    assert rent_msgs[0]["subscription_id"] == 709
    # subscription rents are always incognito → no mode key
    assert "mode" not in rent_msgs[0]

    await client.close()


@pytest.mark.asyncio
async def test_rent_subscription_resolve_failure_raises() -> None:
    """When the backend refuses the subscription rent (403/409), rent() fails
    before touching the relay."""
    from unittest.mock import AsyncMock, patch

    client = Client(
        api_key="testkey",
        relay_url="wss://relay.ceki.me/ws/agent",
        api_url="https://api.ceki.me",
        chat_url="https://chat.ceki.me/api/chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    mock_post = AsyncMock(return_value=_http_response(
        403,
        {
            "message": "Subscription is not active or not allowed",
            "code": "subscription_not_allowed",
        },
    ))

    with patch("httpx.AsyncClient.post", mock_post):
        with pytest.raises(CekiError) as exc_info:
            await client.rent(subscription_id=999)

    assert "not active" in str(exc_info.value)

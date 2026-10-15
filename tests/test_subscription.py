from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ceki_sdk._client import Client
from ceki_sdk._exceptions import CekiError
from ceki_sdk._models import MyBrowsers, SubscriptionInfo

# /api/agent/browsers payload with data[] + subscriptions[].
SAMPLE_MY_BROWSERS = {
    "data": [
        {
            "browser_id": 100,
            "schedule_id": 100,
            "name": "My contract browser",
            "price": 0.0,
            "price_per_min": 0.0,
        }
    ],
    "subscriptions": [
        {
            "subscription_id": 709,
            "type": "group_subscription",
            "group": {"id": 1, "label": "Chrome Browsers", "icon": "/brands/browsers/chrome.svg"},
            "browsers": [
                {
                    "browser_id": 42306,
                    "name": "Chrome Linux",
                    "geo": "RU",
                    "browser_name": "Chrome",
                    "online": True,
                },
                {"browser_id": 43664, "name": "Chrome Win", "geo": "DE", "online": False},
            ],
            "plan_key": "30days",
            "geo_key": "ru",
            "expires_at": "2026-11-09T18:24:42+00:00",
        }
    ],
}


def _make_response(status: int = 200, json_data: dict | list | None = None) -> httpx.Response:
    req = httpx.Request("GET", "http://test")
    return httpx.Response(status, json=json_data, request=req)


def _make_client(api_key: str = "testkey") -> Client:
    return Client(
        api_key=api_key,
        relay_url="wss://relay.ceki.me/ws/agent",
        api_url="https://api.ceki.me",
        chat_url="https://chat.ceki.me/api/chat",
        reconnect=False,
    )


# ── my_browsers() parses subscriptions[] ─────────────────────────────────────


@pytest.mark.asyncio
async def test_my_browsers_parses_subscriptions() -> None:
    client = _make_client()
    mock_get = AsyncMock(return_value=_make_response(200, SAMPLE_MY_BROWSERS))

    with patch("httpx.AsyncClient.get", mock_get):
        result = await client.my_browsers()

    assert isinstance(result, MyBrowsers)
    assert len(result.browsers) == 1
    assert result.browsers[0].schedule_id == 100
    assert len(result.subscriptions) == 1
    sub = result.subscriptions[0]
    assert isinstance(sub, SubscriptionInfo)
    assert sub.subscription_id == 709
    assert sub.group.id == 1
    assert sub.group.label == "Chrome Browsers"
    assert len(sub.browsers) == 2
    assert sub.browsers[0].browser_id == 42306
    assert sub.browsers[0].online is True
    assert sub.browsers[1].online is False
    assert sub.plan_key == "30days"
    assert sub.geo_key == "ru"
    # hit the right endpoint with auth
    assert mock_get.call_args[0][0] == "https://api.ceki.me/api/agent/browsers"
    assert mock_get.call_args[1]["headers"]["Authorization"] == "Bearer testkey"


@pytest.mark.asyncio
async def test_my_browsers_empty_subscriptions() -> None:
    client = _make_client()
    mock_get = AsyncMock(return_value=_make_response(200, {"data": [], "subscriptions": []}))

    with patch("httpx.AsyncClient.get", mock_get):
        result = await client.my_browsers()

    assert result.browsers == []
    assert result.subscriptions == []


# ── ClientSubscription (ceki subscribe, via create-browser-subscription MCP) ─


def _make_subscription_client(api_key: str = "ag_test") -> Client:
    return Client(
        api_key=api_key,
        relay_url="wss://relay.ceki.me/ws/agent",
        api_url="https://api.ceki.me",
        chat_url="https://chat.ceki.me/api/chat",
        reconnect=False,
    )


@pytest.mark.asyncio
async def test_subscription_list_groups() -> None:
    from ceki_sdk.contract import ContractClient

    client = _make_subscription_client(api_key="ag_test")
    payload = {
        "groups": [
            {"group_id": 1, "label": "Chrome Browsers", "icon": "x", "plans": {}, "geo": {}},
            {"group_id": 2, "label": "Yandex Browser", "icon": None, "plans": {}, "geo": {}},
        ],
        "hint": "Call again with group_id + plan_key + geo_key to subscribe.",
    }
    mock_call = MagicMock(return_value=payload)
    with patch.object(ContractClient, "call", mock_call):
        groups = await client.subscription.list_groups()

    assert len(groups) == 2
    assert groups[0]["group_id"] == 1
    assert groups[1]["label"] == "Yandex Browser"
    # discovery mode: no group_id
    assert mock_call.call_args[0][0] == "create-browser-subscription"
    assert mock_call.call_args[0][1] == {}


@pytest.mark.asyncio
async def test_subscription_purchase_sends_group_plan_geo() -> None:
    from ceki_sdk.contract import ContractClient

    client = _make_subscription_client(api_key="ag_test")
    payload = {
        "subscription_id": 999,
        "payment_mode": "paid_from_balance",
        "price": 7.0,
        "currency": "USD",
        "invoice_id": None,
        "note": "Subscription active now.",
    }
    mock_call = MagicMock(return_value=payload)
    with patch.object(ContractClient, "call", mock_call):
        result = await client.subscription.subscribe(1, plan_key="30days", geo_key="ru")

    assert result["subscription_id"] == 999
    assert result["payment_mode"] == "paid_from_balance"
    assert mock_call.call_args[0][0] == "create-browser-subscription"
    assert mock_call.call_args[0][1] == {"group_id": 1, "plan_key": "30days", "geo_key": "ru"}


@pytest.mark.asyncio
async def test_subscription_purchase_error_raises() -> None:
    from ceki_sdk.contract import ContractClient, ContractError

    client = _make_subscription_client(api_key="ag_test")
    mock_call = MagicMock(side_effect=ContractError("AUTH_REQUIRED"))
    with patch.object(ContractClient, "call", mock_call):
        with pytest.raises(CekiError) as exc_info:
            await client.subscription.subscribe(1, plan_key="30days", geo_key="ru")

    assert "subscription error" in str(exc_info.value)

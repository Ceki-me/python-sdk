from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ceki_browser import BrowserOption, connect
from ceki_browser._client import Client

from .conftest import MockRelayServer


def _make_response(data: dict | list) -> httpx.Response:
    req = httpx.Request("GET", "http://test")
    return httpx.Response(200, json=data, request=req)


def _make_client(relay_url: str = "wss://relay.ceki.me/ws/agent") -> Client:
    return Client(api_key="testkey", relay_url=relay_url, reconnect=False)


@pytest.mark.asyncio
async def test_search_url_derived_from_relay() -> None:
    client = _make_client("wss://relay.ceki.me/ws/agent")
    base = (
        client.relay_url.replace("wss://", "https://")
        .replace("ws://", "http://")
        .replace("/ws/agent", "")
    )
    assert base == "https://relay.ceki.me"


@pytest.mark.asyncio
async def test_search_returns_browser_options(mock_relay: MockRelayServer) -> None:
    sample = {
        "schedule_id": 1,
        "geo": "US",
        "languages": ["en"],
        "price_per_min": 0.05,
    }
    mock_resp = _make_response({"data": [sample]})

    with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_resp)):
        url = f"ws://127.0.0.1:{mock_relay.port}"
        client = await connect("testkey", relay_url=url)
        results = await client.search({"geo": "US"}, limit=5)
        assert len(results) == 1
        assert isinstance(results[0], BrowserOption)
        assert results[0].geo == "US"
        assert results[0].price_per_min == 0.05
        await client.close()


@pytest.mark.asyncio
async def test_search_filters_passed_as_params(mock_relay: MockRelayServer) -> None:
    mock_resp = _make_response({"data": []})
    mock_get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.get", mock_get):
        url = f"ws://127.0.0.1:{mock_relay.port}"
        client = await connect("testkey", relay_url=url)
        await client.search({"geo": "DE", "language": "de"}, limit=10)
        await client.close()

    call_kwargs = mock_get.call_args.kwargs
    params = call_kwargs.get("params", {})
    assert params.get("geo") == "DE"
    assert params.get("limit") == 10


@pytest.mark.asyncio
async def test_search_bearer_auth_header(mock_relay: MockRelayServer) -> None:
    mock_resp = _make_response({"data": []})
    mock_get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.get", mock_get):
        url = f"ws://127.0.0.1:{mock_relay.port}"
        client = await connect("my-secret-key", relay_url=url)
        await client.search()
        await client.close()

    headers = mock_get.call_args.kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer my-secret-key"

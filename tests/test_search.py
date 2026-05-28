from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ceki_sdk import BrowserOption, ConnectOptions, connect
from ceki_sdk._client import Client

from .conftest import MockRelayServer


def _make_response(data: dict | list) -> httpx.Response:
    req = httpx.Request("GET", "http://test")
    return httpx.Response(200, json=data, request=req)


def _make_client(relay_url: str = "wss://relay.ceki.me/ws/agent") -> Client:
    return Client(
        api_key="testkey",
        relay_url=relay_url,
        api_url="https://api.ceki.me",
        reconnect=False,
    )


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
        client = await connect("testkey", ConnectOptions(relay_url=url))
        results = await client.search({"geo": "US"}, limit=5)
        assert len(results) == 1
        assert isinstance(results[0], BrowserOption)
        assert results[0].geo == "US"
        assert results[0].price_per_min == 0.05
        await client.close()


@pytest.mark.asyncio
async def test_search_uses_plural_browsers_endpoint(mock_relay: MockRelayServer) -> None:
    mock_resp = _make_response({"data": []})
    mock_get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.get", mock_get):
        url = f"ws://127.0.0.1:{mock_relay.port}"
        client = await connect(
            "testkey",
            ConnectOptions(relay_url=url, api_url="https://clawapi.ittribe.org"),
        )
        await client.search()
        await client.close()

    call_args = mock_get.call_args
    called_url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
    assert "/api/browsers/search" in called_url


@pytest.mark.asyncio
async def test_search_filters_passed_as_params(mock_relay: MockRelayServer) -> None:
    mock_resp = _make_response({"data": []})
    mock_get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.get", mock_get):
        url = f"ws://127.0.0.1:{mock_relay.port}"
        client = await connect("testkey", ConnectOptions(relay_url=url))
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
        client = await connect("my-secret-key", ConnectOptions(relay_url=url))
        await client.search()
        await client.close()

    headers = mock_get.call_args.kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer my-secret-key"


def test_browser_option_laravel_response() -> None:
    raw = {
        "schedule_id": 42,
        "geo": None,
        "language": "en",
        "skills": ["form-fill"],
        "price_per_min": 0.03,
        "currency": "USD",
        "kal_id": 7,
        "rating": 4.5,
    }
    opt = BrowserOption.model_validate(raw)
    assert opt.schedule_id == 42
    assert opt.geo is None
    assert opt.language == "en"
    assert opt.currency == "USD"
    assert opt.kal_id == 7


def test_browser_option_ignores_extra_fields() -> None:
    raw = {
        "schedule_id": 1,
        "price_per_min": 0.05,
        "unknown_field": "ignored",
    }
    opt = BrowserOption.model_validate(raw)
    assert opt.schedule_id == 1


@pytest.mark.asyncio
async def test_rest_uses_bearer_only_even_with_basic_auth(mock_relay: MockRelayServer) -> None:
    """basic_auth must not overwrite Bearer in REST Authorization header."""
    mock_resp = _make_response({"data": []})
    mock_get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.get", mock_get):
        client = await connect(
            "my-api-key",
            ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}", basic_auth=("u", "p")),
        )
        await client.search()
        await client.close()

    headers = mock_get.call_args.kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer my-api-key"


@pytest.mark.asyncio
async def test_ws_uses_basic_auth_in_extra_headers(mock_relay: MockRelayServer) -> None:
    """basic_auth must appear as Authorization: Basic in WS extra_headers."""
    client = await connect(
        "my-api-key",
        ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}", basic_auth=("u", "p")),
    )
    expected = "Basic " + base64.b64encode(b"u:p").decode()
    assert client._ws_extra_headers().get("Authorization") == expected
    await client.close()

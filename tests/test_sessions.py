from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ceki_browser import Client, ConnectOptions, SessionInfo, connect

from .conftest import MockRelayServer

MOCK_SESSIONS_RESPONSE = {
    "data": [
        {
            "id": 2650,
            "schedule_id": 703,
            "started_at": "2026-05-18T10:43:09Z",
            "ended_at": None,
            "status": "active",
            "duration": 148,
            "earned": 0.25,
            "price_per_min": 0.10,
            "renter": {"type": "agent", "id": 4, "name": "First"},
            "provider": {"type": "user", "id": 1, "name": "Konstantin"},
            "data": {"chat_topic_id": "topic-abc"},
        },
        {
            "id": 2651,
            "schedule_id": 704,
            "started_at": "2026-05-18T11:00:00Z",
            "ended_at": None,
            "status": "active",
            "duration": 60,
            "earned": 0.10,
            "price_per_min": 0.10,
            "renter": {"type": "agent", "id": 5, "name": "Second"},
            "provider": {"type": "user", "id": 2, "name": "Alice"},
            "data": {},
        },
    ]
}


def _patch_httpx_get(json_body=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json = Mock(return_value=json_body or MOCK_SESSIONS_RESPONSE)
    resp.raise_for_status = Mock()
    client_mock = AsyncMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    client_mock.get = AsyncMock(return_value=resp)
    return patch("httpx.AsyncClient", return_value=client_mock), client_mock


@pytest.mark.asyncio
async def test_list_sessions_active_only(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url, api_url="http://localhost:9999"))
    try:
        patcher, http_mock = _patch_httpx_get()
        with patcher:
            results = await client.list_sessions(active=True, limit=50)
        assert len(results) == 2
        assert all(isinstance(r, SessionInfo) for r in results)
        assert results[0].id == 2650
        assert results[0].schedule_id == 703
        assert results[0].status == "active"
        assert results[0].renter["name"] == "First"

        call_args = http_mock.get.call_args
        assert "active" in str(call_args)
    finally:
        if client._ws:
            await client.disconnect()


@pytest.mark.asyncio
async def test_list_sessions_all(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url, api_url="http://localhost:9999"))
    try:
        patcher, http_mock = _patch_httpx_get()
        with patcher:
            results = await client.list_sessions(active=False)
        call_kwargs = http_mock.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params.get("active") == "0"
    finally:
        if client._ws:
            await client.disconnect()


@pytest.mark.asyncio
async def test_list_sessions_empty(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url, api_url="http://localhost:9999"))
    try:
        patcher, _ = _patch_httpx_get(json_body={"data": []})
        with patcher:
            results = await client.list_sessions()
        assert results == []
    finally:
        if client._ws:
            await client.disconnect()

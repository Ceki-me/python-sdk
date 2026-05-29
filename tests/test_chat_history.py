from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ceki_sdk import ConnectOptions, connect
from ceki_sdk._models import ChatMessage


def _make_response(data) -> httpx.Response:
    resp = httpx.Response(200, json=data)
    resp.request = httpx.Request("GET", "http://test")
    return resp


@pytest.fixture
async def chat_browser(mock_relay):
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))

    async def ack_rent():
        server_ev = "ev-test-1"
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": server_ev})
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": server_ev,
            "session_id": "sess-hist",
            "browser_id": 1,
            "chat_topic_id": "55",
            "browser_info": {},
        })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t
    yield browser, mock_relay
    await client.close()


@pytest.mark.asyncio
async def test_history_returns_messages_asc(chat_browser):
    browser, _ = chat_browser

    def _msg(mid, sid, text, ts):
        return {"_id": str(mid), "topic_id": "55", "sender_id": sid,
                "text": text, "type": "text", "created_at": ts}

    messages_data = [
        _msg(1, 1, "first", "2026-05-07T10:00:00.000Z"),
        _msg(2, 7, "second", "2026-05-07T10:01:00.000Z"),
        _msg(3, 1, "third", "2026-05-07T10:02:00.000Z"),
    ]

    mock_resp = _make_response({"messages": messages_data})
    with patch("httpx.AsyncClient.send", new_callable=AsyncMock, return_value=mock_resp):
        history = await browser.chat.history(limit=3)

    assert len(history) == 3
    assert [m.id for m in history] == ["1", "2", "3"]
    assert all(isinstance(m, ChatMessage) for m in history)


@pytest.mark.asyncio
async def test_history_passes_limit_param(chat_browser):
    browser, _ = chat_browser

    captured_request: list[httpx.Request] = []

    async def mock_send(request: httpx.Request, **kwargs):
        captured_request.append(request)
        resp = _make_response({"data": []})
        resp.request = request
        return resp

    with patch("httpx.AsyncClient.send", new_callable=AsyncMock, side_effect=mock_send):
        await browser.chat.history(limit=10)

    assert len(captured_request) == 1
    url_str = str(captured_request[0].url)
    assert "limit=10" in url_str
    assert "topic_id=" in url_str
    assert "/messages" in url_str


@pytest.mark.asyncio
async def test_history_passes_before_id_param(chat_browser):
    browser, _ = chat_browser

    captured_request: list[httpx.Request] = []

    async def mock_send(request: httpx.Request, **kwargs):
        captured_request.append(request)
        resp = _make_response({"data": []})
        resp.request = request
        return resp

    with patch("httpx.AsyncClient.send", new_callable=AsyncMock, side_effect=mock_send):
        await browser.chat.history(limit=5, before_id=100)

    url_str = str(captured_request[0].url)
    assert "before=100" in url_str
    assert "limit=5" in url_str
    assert "topic_id=" in url_str
    assert "/messages" in url_str


@pytest.mark.asyncio
async def test_history_no_topic_returns_empty(mock_relay):
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))

    async def ack_rent():
        server_ev = "ev-test-2"
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": server_ev})
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": server_ev,
            "session_id": "sess-notopic",
            "browser_id": 1,
            "chat_topic_id": None,
            "browser_info": {},
        })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t

    result = await browser.chat.history()
    assert result == []

    await client.close()

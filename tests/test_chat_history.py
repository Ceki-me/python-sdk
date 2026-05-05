from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ceki_browser import connect
from ceki_browser._models import ChatMessage


def _make_response(data) -> httpx.Response:
    resp = httpx.Response(200, json=data)
    resp.request = httpx.Request("GET", "http://test")
    return resp


@pytest.fixture
async def chat_browser(mock_relay):
    client = await connect("test-key", relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent")

    async def ack_rent():
        await asyncio.sleep(0.05)
        rent = next((m for m in mock_relay.received if m.get("type") == "rent"), None)
        if rent:
            await mock_relay.send_to_all({
                "type": "match",
                "event_id": rent["event_id"],
                "session_id": "sess-hist",
                "schedule_id": 1,
                "chat_topic_id": 55,
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

    def _msg(mid, stype, sid, text, ts):
        return {"message_id": mid, "sender_type": stype, "sender_id": sid,
                "text": text, "image_url": None, "sent_at": ts}

    messages_data = [
        _msg(1, "agent", 1, "first", 1746441600.0),
        _msg(2, "provider", 7, "second", 1746441660.0),
        _msg(3, "agent", 1, "third", 1746441720.0),
    ]

    mock_resp = _make_response({"data": messages_data})
    with patch("httpx.AsyncClient.send", new_callable=AsyncMock, return_value=mock_resp):
        history = await browser.chat.history(limit=3)

    assert len(history) == 3
    assert [m.message_id for m in history] == [1, 2, 3]
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
    assert "limit=10" in str(captured_request[0].url)


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
    assert "before_id=100" in url_str
    assert "limit=5" in url_str


@pytest.mark.asyncio
async def test_history_no_topic_returns_empty(mock_relay):
    client = await connect("test-key", relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent")

    async def ack_rent():
        await asyncio.sleep(0.05)
        rent = next((m for m in mock_relay.received if m.get("type") == "rent"), None)
        if rent:
            await mock_relay.send_to_all({
                "type": "match",
                "event_id": rent["event_id"],
                "session_id": "sess-notopic",
                "schedule_id": 1,
                "chat_topic_id": None,
                "browser_info": {},
            })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t

    result = await browser.chat.history()
    assert result == []

    await client.close()

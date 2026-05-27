from __future__ import annotations

import asyncio

import pytest

from ceki_browser import ConnectOptions, connect
from ceki_browser._models import ChatMessage, ReadReceipt


@pytest.fixture
async def chat_browser(mock_relay):
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))

    async def ack_rent():
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": "ev-chat", "schedule_id": 1})
        await asyncio.sleep(0.02)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": "ev-chat",
            "session_id": "sess-chat",
            "schedule_id": 1,
            "chat_topic_id": "77",
            "browser_info": {},
        })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t
    yield browser, mock_relay
    await client.close()


@pytest.fixture
async def chat_browser_no_topic(mock_relay):
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))

    async def ack_rent():
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": "ev-notopic", "schedule_id": 1})
        await asyncio.sleep(0.02)
        await mock_relay.send_to_all({
            "type": "match",
            "event_id": "ev-notopic",
            "session_id": "sess-notopic",
            "schedule_id": 1,
            "chat_topic_id": None,
            "browser_info": {},
        })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t
    yield browser, mock_relay
    await client.close()


@pytest.mark.asyncio
async def test_send_text_ack(chat_browser):
    browser, mock_relay = chat_browser

    async def ack_send():
        await asyncio.sleep(0.05)
        send_msg = next((m for m in mock_relay.received if m.get("type") == "chat.send"), None)
        assert send_msg is not None
        assert send_msg["text"] == "hello"
        assert send_msg["session_id"] == "sess-chat"
        client_msg_id = send_msg["client_msg_id"]
        await mock_relay.send_to_all({
            "type": "chat.send_ack",
            "session_id": "sess-chat",
            "client_msg_id": client_msg_id,
            "message_id": 42,
            "sent_at": "2026-05-05T10:00:00Z",
        })

    t = asyncio.create_task(ack_send())
    result = await browser.chat.send("hello")
    await t

    assert result["message_id"] == 42
    assert result["sent_at"] == "2026-05-05T10:00:00Z"


@pytest.mark.asyncio
async def test_on_message_callback(chat_browser):
    browser, mock_relay = chat_browser

    received: list[ChatMessage] = []

    async def on_msg(msg: ChatMessage) -> None:
        received.append(msg)

    browser.chat.on_message(on_msg)

    await mock_relay.send_to_all({
        "type": "chat.message",
        "session_id": "sess-chat",
        "topic_id": "77",
        "message": {
            "_id": "69fcbb000000000000000001",
            "topic_id": "77",
            "sender_id": 7,
            "text": "captcha solved",
            "media": None,
            "type": "text",
            "created_at": "2026-05-07T10:00:00.000Z",
            "edited_at": None,
            "deleted_at": None,
        },
    })

    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].id == "69fcbb000000000000000001"
    assert received[0].sender_id == 7
    assert received[0].text == "captcha solved"
    assert received[0].type == "text"
    assert not received[0].is_system()
    assert received[0].is_from_provider(7)
    assert not received[0].is_from_provider(99)


@pytest.mark.asyncio
async def test_on_read_callback(chat_browser):
    browser, mock_relay = chat_browser

    receipts: list[ReadReceipt] = []

    async def on_read(receipt: ReadReceipt) -> None:
        receipts.append(receipt)

    browser.chat.on_read(on_read)

    await mock_relay.send_to_all({
        "type": "chat.read",
        "session_id": "sess-chat",
        "payload": {
            "topic_id": "77",
            "last_read_message_id": "69fcbb000000000000000042",
            "read_at": 1746441720.0,
        },
    })

    await asyncio.sleep(0.1)

    assert len(receipts) == 1
    assert receipts[0].last_read_message_id == "69fcbb000000000000000042"
    assert receipts[0].topic_id == "77"


@pytest.mark.asyncio
async def test_send_without_topic_raises(chat_browser_no_topic):
    browser, _ = chat_browser_no_topic

    with pytest.raises(RuntimeError, match="chat topic not assigned"):
        await browser.chat.send("hello")


# --- Unit tests for ChatMessage v2 schema and helpers ---

def _make_msg(**kwargs):
    base = {
        "_id": "69fcbb000000000000000001",
        "topic_id": "69fcbb000000000000000002",
        "sender_id": 1,
        "text": "hello",
        "type": "text",
        "created_at": "2026-05-07T10:00:00.000Z",
    }
    base.update(kwargs)
    return ChatMessage.model_validate(base)


def test_chatmessage_validates_real_payload():
    msg = _make_msg(media=None, edited_at=None, deleted_at=None)
    assert msg.id == "69fcbb000000000000000001"
    assert msg.topic_id == "69fcbb000000000000000002"
    assert msg.sender_id == 1
    assert msg.text == "hello"
    assert msg.type == "text"
    assert msg.created_at == "2026-05-07T10:00:00.000Z"


def test_chatmessage_type_system():
    msg = _make_msg(type="system", text="[ext v0.6.63]\n[10:00:00][info] session start")
    assert msg.is_system()
    assert not msg.is_from_provider(None)
    assert not msg.is_from_provider(99)


def test_chatmessage_type_text_default():
    base = {
        "_id": "69fcbb000000000000000003",
        "topic_id": "69fcbb000000000000000002",
        "sender_id": 2,
        "text": "hi",
        "created_at": "2026-05-07T10:01:00.000Z",
    }
    msg = ChatMessage.model_validate(base)
    assert msg.type == "text"
    assert not msg.is_system()


def test_chatmessage_is_from_provider():
    msg = _make_msg(sender_id=5)
    assert msg.is_from_provider(5)
    assert not msg.is_from_provider(6)
    assert not msg.is_from_provider(None)


def test_chatmessage_extra_fields_ignored():
    base = {
        "_id": "69fcbb000000000000000004",
        "topic_id": "69fcbb000000000000000002",
        "sender_id": 1,
        "text": "x",
        "created_at": "2026-05-07T10:02:00.000Z",
        "unknown_future_field": "value",
    }
    msg = ChatMessage.model_validate(base)
    assert msg.text == "x"


def test_match_provider_user_id():
    from ceki_browser._models import Match
    m = Match.model_validate({
        "session_id": "sess-1",
        "schedule_id": 240,
        "event_id": "ev-1",
        "chat_topic_id": "69fcbb000000000000000002",
        "provider_user_id": 3,
        "browser_info": {},
    })
    assert m.provider_user_id == 3


def test_match_provider_user_id_missing():
    from ceki_browser._models import Match
    m = Match.model_validate({
        "session_id": "sess-2",
        "schedule_id": 240,
    })
    assert m.provider_user_id is None

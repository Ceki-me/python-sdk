import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_browser.chat import ChatAPI
from ceki_browser.transport import Transport
from ceki_browser.types import ChatMessage, TypingEvent


class FakeWebSocket:
    def __init__(self, messages: list[str] | None = None):
        self._messages = list(messages or [])
        self._sent: list[str] = []
        self._closed = False
        self.state = MagicMock()
        self.state.name = "OPEN"

    async def recv(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(100)
        return ""

    async def send(self, data: str) -> None:
        self._sent.append(data)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        if self._closed:
            raise StopAsyncIteration
        await asyncio.sleep(100)
        raise StopAsyncIteration


WELCOME = json.dumps({"jsonrpc": "2.0", "result": {"status": "connected", "agent_id": "a-1"}, "id": 0})


@pytest.fixture
def transport_and_ws():
    send_resp = json.dumps({
        "jsonrpc": "2.0",
        "result": {"message_id": "msg-001", "created_at": "2026-04-28T12:00:00Z", "persisted": True},
        "id": 1,
    })
    ws = FakeWebSocket([WELCOME, send_resp])
    return ws


@pytest.mark.asyncio
async def test_chat_send(transport_and_ws):
    ws = transport_and_ws
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", "topic-1")
        msg = await chat.send("hello")

        assert msg._id == "msg-001"
        assert msg.content == "hello"
        assert msg.type == "text"

        sent = json.loads(ws._sent[0])
        assert sent["method"] == "chat.send"
        assert sent["params"]["session_id"] == "sess-1"
        assert sent["params"]["content"] == "hello"
        assert sent["params"]["type"] == "text"
        await t.close()


@pytest.mark.asyncio
async def test_chat_send_image(transport_and_ws):
    ws = transport_and_ws
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", "topic-1")
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        msg = await chat.send_image(png_bytes, "image/png")

        assert msg._id == "msg-001"
        assert msg.type == "image"

        sent = json.loads(ws._sent[0])
        assert sent["params"]["type"] == "image"
        assert "data" in sent["params"]["media"]
        assert sent["params"]["media"]["mime"] == "image/png"
        await t.close()


@pytest.mark.asyncio
async def test_chat_history():
    history_resp = json.dumps({
        "jsonrpc": "2.0",
        "result": {
            "messages": [
                {"_id": "m1", "topic_id": "t1", "author_id": 1, "author_name": "Agent", "type": "text", "content": "hi", "created_at": "2026-04-28T11:00:00Z"},
                {"_id": "m2", "topic_id": "t1", "author_id": 2, "author_name": "Provider", "type": "text", "content": "hello", "created_at": "2026-04-28T11:01:00Z"},
            ],
            "has_more": False,
            "next_cursor": None,
        },
        "id": 1,
    })
    ws = FakeWebSocket([WELCOME, history_resp])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", "topic-1")
        messages = await chat.history()

        assert len(messages) == 2
        assert messages[0]._id == "m1"
        assert messages[0].content == "hi"
        assert messages[1].author_name == "Provider"
        await t.close()


@pytest.mark.asyncio
async def test_chat_history_no_topic():
    ws = FakeWebSocket([WELCOME])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", None)
        messages = await chat.history()
        assert messages == []
        await t.close()


@pytest.mark.asyncio
async def test_chat_on_message():
    ws = FakeWebSocket([WELCOME])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", "topic-1")
        received: list[ChatMessage] = []
        unsub = chat.on_message(received.append)

        chat._dispatch_message({
            "message": {
                "_id": "m3",
                "topic_id": "topic-1",
                "author_id": 99,
                "author_name": "Provider",
                "type": "text",
                "content": "captcha please",
                "created_at": "2026-04-28T12:00:00Z",
            }
        })

        assert len(received) == 1
        assert received[0].content == "captcha please"
        assert received[0].author_id == 99

        unsub()
        chat._dispatch_message({"message": {"_id": "m4", "content": "ignored"}})
        assert len(received) == 1
        await t.close()


@pytest.mark.asyncio
async def test_chat_on_typing():
    ws = FakeWebSocket([WELCOME])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", "topic-1")
        events: list[TypingEvent] = []
        unsub = chat.on_typing(events.append)

        chat._dispatch_typing({"user_id": 42, "is_typing": True})

        assert len(events) == 1
        assert events[0].user_id == 42
        assert events[0].is_typing is True

        unsub()
        await t.close()


@pytest.mark.asyncio
async def test_chat_typing_sends_notification():
    ws = FakeWebSocket([WELCOME])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="tok", relay_url="wss://test/ws/agent")
        await t.connect()

        chat = ChatAPI(t, "sess-1", "topic-1")
        await chat.typing(True)

        sent = json.loads(ws._sent[0])
        assert sent["method"] == "chat.typing"
        assert sent["params"]["is_typing"] is True
        assert sent["params"]["session_id"] == "sess-1"
        assert "id" not in sent
        await t.close()

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_browser.errors import AuthError, CommandTimeout, RateLimited
from ceki_browser.transport import Transport


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


@pytest.fixture
def welcome_msg():
    return json.dumps({"jsonrpc": "2.0", "result": {"status": "connected", "agent_id": "agent-123"}, "id": 0})


@pytest.mark.asyncio
async def test_connect_success(welcome_msg):
    ws = FakeWebSocket([welcome_msg])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="test-token", relay_url="wss://relay.test/ws/agent")
        result = await t.connect()
        assert result["agent_id"] == "agent-123"
        assert t.agent_id == "agent-123"
        await t.close()


@pytest.mark.asyncio
async def test_connect_auth_error():
    welcome = json.dumps({"jsonrpc": "2.0", "error": {"code": 401, "message": "Unauthorized"}, "id": 0})
    ws = FakeWebSocket([welcome])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="bad-token", relay_url="wss://relay.test/ws/agent")
        with pytest.raises(AuthError, match="Unauthorized"):
            await t.connect()
        await t.close()


@pytest.mark.asyncio
async def test_send_receive_roundtrip(welcome_msg):
    response = json.dumps({"jsonrpc": "2.0", "result": {"url": "https://example.com", "title": "Example"}, "id": 1})
    ws = FakeWebSocket([welcome_msg, response])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="test-token", relay_url="wss://relay.test/ws/agent")
        await t.connect()
        result = await t.send("browser.navigate", {"url": "https://example.com"})
        assert result["url"] == "https://example.com"

        sent = json.loads(ws._sent[0])
        assert sent["method"] == "browser.navigate"
        assert sent["id"] == 1
        await t.close()


@pytest.mark.asyncio
async def test_error_mapping(welcome_msg):
    error_resp = json.dumps({"jsonrpc": "2.0", "error": {"code": -1013, "message": "Rate limit exceeded"}, "id": 1})
    ws = FakeWebSocket([welcome_msg, error_resp])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="test-token", relay_url="wss://relay.test/ws/agent")
        await t.connect()
        with pytest.raises(RateLimited, match="Rate limit"):
            await t.send("session.request", {"mode": "incognito"})
        await t.close()


@pytest.mark.asyncio
async def test_notification_dispatch(welcome_msg):
    notification = json.dumps({"jsonrpc": "2.0", "method": "session.state_changed", "params": {"state": "ACTIVE"}})
    ws = FakeWebSocket([welcome_msg, notification])

    events: list[tuple[str, dict]] = []

    async def on_event(method: str, params: dict) -> None:
        events.append((method, params))

    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="test-token", relay_url="wss://relay.test/ws/agent")
        t.on_event(on_event)
        await t.connect()
        await asyncio.sleep(0.1)
        assert len(events) == 1
        assert events[0][0] == "session.state_changed"
        assert events[0][1]["state"] == "ACTIVE"
        await t.close()


@pytest.mark.asyncio
async def test_command_timeout(welcome_msg):
    ws = FakeWebSocket([welcome_msg])
    with patch("ceki_browser.transport.websockets.connect", new_callable=AsyncMock, return_value=ws):
        t = Transport(token="test-token", relay_url="wss://relay.test/ws/agent")
        await t.connect()
        with pytest.raises(CommandTimeout):
            await t.send("browser.navigate", {"url": "https://slow.test"}, timeout=0.1)
        await t.close()

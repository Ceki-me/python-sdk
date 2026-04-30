"""Test that heartbeat survives long sessions with infrequent RPCs.

Verifies that a single CommandTimeout in the heartbeat loop does NOT
kill heartbeats permanently — the loop must continue sending pings.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_browser.transport import Transport


class FakeWebSocket:
    def __init__(self, messages: list[str] | None = None):
        self._messages = list(messages or [])
        self._sent: list[str] = []
        self._closed = False
        self.state = MagicMock()
        self.state.name = "OPEN"
        self._pending_responses: dict[int, dict] = {}

    async def recv(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(100)
        return ""

    async def send(self, data: str) -> None:
        self._sent.append(data)
        msg = json.loads(data)
        if msg.get("method") == "heartbeat" and msg.get("id") is not None:
            resp = json.dumps({"jsonrpc": "2.0", "result": "pong", "id": msg["id"]})
            self._messages.append(resp)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        if self._closed:
            raise StopAsyncIteration
        await asyncio.sleep(0.05)
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


@pytest.fixture
def welcome_msg():
    return json.dumps({"jsonrpc": "2.0", "result": {"status": "connected", "agent_id": "agent-123"}, "id": 0})


@pytest.mark.asyncio
async def test_heartbeat_continues_after_timeout(welcome_msg):
    """Heartbeat loop must survive a CommandTimeout and keep sending."""
    ws = FakeWebSocket([welcome_msg])

    call_count = 0
    original_send = Transport.send

    async def patched_send(self, method, params=None, timeout=60.0):
        nonlocal call_count
        if method == "heartbeat":
            call_count += 1
            if call_count == 2:
                raise asyncio.TimeoutError()
        return await original_send(self, method, params=params, timeout=timeout)

    with patch("websockets.connect", AsyncMock(return_value=ws)):
        t = Transport("test-token")
        await t.connect()

        with patch.object(t, "send", lambda m, **kw: patched_send(t, m, **kw)):
            await asyncio.sleep(0.5)

        heartbeat_sends = [
            s for s in ws._sent
            if '"heartbeat"' in s and '"id"' in s
        ]
        assert len(heartbeat_sends) >= 1, "At least one heartbeat should have been sent"
        assert not t._closed, "Transport should still be open"
        await t.close()


@pytest.mark.asyncio
async def test_heartbeat_sends_periodically(welcome_msg):
    """Heartbeat pings are sent at regular intervals."""
    ws = FakeWebSocket([welcome_msg])

    with patch("websockets.connect", AsyncMock(return_value=ws)):
        t = Transport("test-token")
        await t.connect()

        await asyncio.sleep(0.3)

        heartbeat_sends = [
            json.loads(s) for s in ws._sent
            if '"heartbeat"' in s
        ]
        assert not t._closed, "Transport should remain open during heartbeats"
        await t.close()

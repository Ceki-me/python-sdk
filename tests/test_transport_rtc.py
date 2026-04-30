import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_browser.transport_rtc import RTCTransport


class MockDataChannel:
    def __init__(self, label: str):
        self.label = label
        self.readyState = "open"
        self._sent: list[str] = []
        self._handlers: dict[str, list] = {}

    def send(self, data: str) -> None:
        self._sent.append(data)

    def on(self, event: str):
        def decorator(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return decorator

    def emit(self, event: str, *args):
        for h in self._handlers.get(event, []):
            h(*args)


def make_transport_with_mock_channels():
    with patch("ceki_browser.transport_rtc.RTCPeerConnection") as MockPC:
        pc = MagicMock()
        pc.connectionState = "new"
        pc.iceGatheringState = "new"
        pc._handlers = {}

        def on_decorator(event):
            def decorator(fn):
                pc._handlers.setdefault(event, []).append(fn)
                return fn
            return decorator

        pc.on = on_decorator

        cmd_ch = MockDataChannel("ceki-cmd")
        pc.createDataChannel = MagicMock(return_value=cmd_ch)
        pc.close = AsyncMock()
        MockPC.return_value = pc

        transport = RTCTransport([{"urls": "stun:stun.l.google.com:19302"}])
        transport.cmd_channel = cmd_ch

        return transport, pc, cmd_ch


@pytest.mark.asyncio
async def test_send_command_roundtrip():
    transport, pc, cmd_ch = make_transport_with_mock_channels()

    async def respond():
        await asyncio.sleep(0.05)
        sent = json.loads(cmd_ch._sent[0])
        response = json.dumps({"jsonrpc": "2.0", "result": {"url": "https://example.com"}, "id": sent["id"]})
        cmd_ch.emit("message", response)

    task = asyncio.create_task(respond())
    result = await transport.send_command("browser.navigate", {"url": "https://example.com"}, timeout=2.0)
    await task

    assert result["url"] == "https://example.com"
    sent = json.loads(cmd_ch._sent[0])
    assert sent["method"] == "browser.navigate"
    assert sent["params"]["url"] == "https://example.com"
    await transport.close()


@pytest.mark.asyncio
async def test_send_command_error():
    transport, pc, cmd_ch = make_transport_with_mock_channels()

    async def respond():
        await asyncio.sleep(0.05)
        sent = json.loads(cmd_ch._sent[0])
        err = {"code": -1010, "message": "Provider disconnected"}
        response = json.dumps({"jsonrpc": "2.0", "error": err, "id": sent["id"]})
        cmd_ch.emit("message", response)

    from ceki_browser.errors import CekiBrowserError

    task = asyncio.create_task(respond())
    with pytest.raises(CekiBrowserError, match="Provider disconnected"):
        await transport.send_command("browser.screenshot", timeout=2.0)
    await task
    await transport.close()

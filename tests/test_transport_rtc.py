import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_browser.transport_rtc import (
    CHUNK_SIZE,
    ChatImage,
    ChatTextMessage,
    RTCTransport,
)


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
        chat_ch = MockDataChannel("ceki-chat")
        pc.createDataChannel = MagicMock(side_effect=[cmd_ch, chat_ch])
        pc.close = AsyncMock()
        MockPC.return_value = pc

        transport = RTCTransport([{"urls": "stun:stun.l.google.com:19302"}])
        transport.cmd_channel = cmd_ch
        transport.chat_channel = chat_ch

        return transport, pc, cmd_ch, chat_ch


@pytest.mark.asyncio
async def test_send_command_roundtrip():
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

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
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

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


@pytest.mark.asyncio
async def test_chat_send_receive():
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

    received: list[ChatTextMessage] = []
    transport.on_chat_message(received.append)

    await transport.send_chat_text("hello from agent")

    assert len(chat_ch._sent) == 1
    sent = json.loads(chat_ch._sent[0])
    assert sent["type"] == "msg"
    assert sent["text"] == "hello from agent"
    assert sent["from"] == "agent"

    incoming = json.dumps({
        "type": "msg",
        "id": "msg-from-provider",
        "from": "provider",
        "ts": int(time.time() * 1000),
        "text": "hello from provider",
    })
    chat_ch.emit("message", incoming)

    assert len(received) == 1
    assert received[0].text == "hello from provider"
    assert received[0].from_ == "provider"

    assert len(transport.chat_history) == 2
    await transport.close()


@pytest.mark.asyncio
async def test_chat_image_chunked_roundtrip():
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

    raw = b"\x89PNG" + bytes(range(256)) * 800

    received_images: list[ChatImage] = []
    transport.on_chat_image(received_images.append)

    b64 = base64.b64encode(raw).decode("ascii")
    total_chunks = (len(b64) + CHUNK_SIZE - 1) // CHUNK_SIZE

    chat_ch.emit("message", json.dumps({
        "type": "img-start",
        "id": "img-1",
        "from": "provider",
        "ts": 1000,
        "mime": "image/png",
        "size_bytes": len(raw),
        "total_chunks": total_chunks,
    }))

    for i in range(total_chunks):
        chunk = b64[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        chat_ch.emit("message", json.dumps({
            "type": "img-chunk",
            "id": "img-1",
            "seq": i,
            "data": chunk,
        }))

    chat_ch.emit("message", json.dumps({"type": "img-end", "id": "img-1"}))

    assert len(received_images) == 1
    assert received_images[0].data == raw
    assert received_images[0].mime == "image/png"
    assert received_images[0].from_ == "provider"
    await transport.close()


@pytest.mark.asyncio
async def test_chat_image_oversize_rejected():
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

    raw = b"\x00" * (6 * 1024 * 1024)

    with pytest.raises(ValueError, match="too large"):
        await transport.send_chat_image(raw, "image/png")
    await transport.close()


@pytest.mark.asyncio
async def test_chat_image_assembler_timeout():
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

    chat_ch.emit("message", json.dumps({
        "type": "img-start",
        "id": "img-timeout",
        "from": "provider",
        "ts": 1000,
        "mime": "image/png",
        "size_bytes": 1000,
        "total_chunks": 5,
    }))

    assert "img-timeout" in transport._assemblers

    transport._assembler_timeout("img-timeout")

    assert "img-timeout" not in transport._assemblers
    await transport.close()


@pytest.mark.asyncio
async def test_session_end_clears_history():
    transport, pc, cmd_ch, chat_ch = make_transport_with_mock_channels()

    await transport.send_chat_text("msg1")
    await transport.send_chat_text("msg2")
    assert len(transport.chat_history) == 2

    await transport.close()
    assert len(transport.chat_history) == 0

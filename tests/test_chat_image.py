from __future__ import annotations

import asyncio
import base64

import pytest

from ceki_browser import connect
from ceki_browser._chat import MAX_IMAGE_BYTES

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 100
WEBP_MAGIC = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100


@pytest.fixture
async def chat_browser(mock_relay, tmp_path):
    client = await connect("test-key", relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent")

    async def ack_rent():
        await asyncio.sleep(0.05)
        rent = next((m for m in mock_relay.received if m.get("type") == "rent"), None)
        if rent:
            await mock_relay.send_to_all({
                "type": "match",
                "event_id": rent["event_id"],
                "session_id": "sess-img",
                "schedule_id": 1,
                "chat_topic_id": 88,
                "browser_info": {},
            })

    t = asyncio.create_task(ack_rent())
    browser = await client.rent(1)
    await t
    yield browser, mock_relay, tmp_path
    await client.close()


async def _ack_image_send(mock_relay):
    await asyncio.sleep(0.05)
    send_msg = next((m for m in mock_relay.received if m.get("type") == "chat.send_image"), None)
    assert send_msg is not None
    client_msg_id = send_msg["client_msg_id"]
    await mock_relay.send_to_all({
        "type": "chat.send_ack",
        "session_id": "sess-img",
        "client_msg_id": client_msg_id,
        "message_id": 1,
        "sent_at": "2026-05-05T10:00:00Z",
    })
    return send_msg


@pytest.mark.asyncio
async def test_send_image_png_mime_detect(chat_browser):
    browser, mock_relay, _ = chat_browser

    t = asyncio.create_task(_ack_image_send(mock_relay))
    await browser.chat.send_image(PNG_MAGIC)
    sent = await t

    assert sent["mime"] == "image/png"
    assert sent["base64"] == base64.b64encode(PNG_MAGIC).decode()


@pytest.mark.asyncio
async def test_send_image_jpeg_mime_detect(chat_browser):
    browser, mock_relay, _ = chat_browser

    t = asyncio.create_task(_ack_image_send(mock_relay))
    await browser.chat.send_image(JPEG_MAGIC)
    sent = await t

    assert sent["mime"] == "image/jpeg"


@pytest.mark.asyncio
async def test_send_image_webp_mime_detect(chat_browser):
    browser, mock_relay, _ = chat_browser

    t = asyncio.create_task(_ack_image_send(mock_relay))
    await browser.chat.send_image(WEBP_MAGIC)
    sent = await t

    assert sent["mime"] == "image/webp"


@pytest.mark.asyncio
async def test_send_image_from_path_jpeg(chat_browser):
    browser, mock_relay, tmp_path = chat_browser

    img_file = tmp_path / "photo.jpg"
    img_file.write_bytes(JPEG_MAGIC)

    t = asyncio.create_task(_ack_image_send(mock_relay))
    await browser.chat.send_image(img_file)
    sent = await t

    assert sent["mime"] == "image/jpeg"


@pytest.mark.asyncio
async def test_send_image_from_str_path(chat_browser):
    browser, mock_relay, tmp_path = chat_browser

    img_file = tmp_path / "photo.png"
    img_file.write_bytes(PNG_MAGIC)

    t = asyncio.create_task(_ack_image_send(mock_relay))
    await browser.chat.send_image(str(img_file))
    sent = await t

    assert sent["mime"] == "image/png"


@pytest.mark.asyncio
async def test_send_image_size_limit(chat_browser):
    browser, mock_relay, _ = chat_browser

    big_data = b"\x89PNG" + b"\x00" * (MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValueError, match="too large"):
        await browser.chat.send_image(big_data)


@pytest.mark.asyncio
async def test_send_image_mime_override(chat_browser):
    browser, mock_relay, _ = chat_browser

    t = asyncio.create_task(_ack_image_send(mock_relay))
    await browser.chat.send_image(PNG_MAGIC, mime="image/webp")
    sent = await t

    assert sent["mime"] == "image/webp"

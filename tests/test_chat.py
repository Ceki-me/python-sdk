import time

import pytest

from ceki_browser.session import ChatAPI
from ceki_browser.transport_rtc import ChatImage, ChatTextMessage


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


class MockRTC:
    def __init__(self):
        self.cmd_channel = MockDataChannel("ceki-cmd")
        self.chat_channel = MockDataChannel("ceki-chat")
        self._chat_text_handlers: list = []
        self._chat_image_handlers: list = []
        self._chat_history: list = []
        self._sent_texts: list[str] = []
        self._sent_images: list[tuple] = []

    async def send_chat_text(self, text: str):
        self._sent_texts.append(text)
        msg = ChatTextMessage(id=f"msg-{len(self._sent_texts)}", from_="agent", ts=int(time.time() * 1000), text=text)
        self._chat_history.append(msg)

    async def send_chat_image(self, data, mime=None):
        self._sent_images.append((data, mime))

    def on_chat_message(self, cb):
        self._chat_text_handlers.append(cb)

    def on_chat_image(self, cb):
        self._chat_image_handlers.append(cb)

    @property
    def chat_history(self):
        return list(self._chat_history)

    def _dispatch_text(self, msg: ChatTextMessage):
        self._chat_history.append(msg)
        for h in self._chat_text_handlers:
            h(msg)

    def _dispatch_image(self, img: ChatImage):
        self._chat_history.append(img)
        for h in self._chat_image_handlers:
            h(img)


@pytest.mark.asyncio
async def test_chat_send_text():
    rtc = MockRTC()
    chat = ChatAPI(rtc)

    await chat.send("hello world")
    assert rtc._sent_texts == ["hello world"]
    assert len(rtc.chat_history) == 1


@pytest.mark.asyncio
async def test_chat_send_image():
    rtc = MockRTC()
    chat = ChatAPI(rtc)

    png = b"\x89PNG" + b"\x00" * 100
    await chat.send_image(png, "image/png")
    assert len(rtc._sent_images) == 1
    assert rtc._sent_images[0][1] == "image/png"


@pytest.mark.asyncio
async def test_chat_on_message_callback():
    rtc = MockRTC()
    chat = ChatAPI(rtc)

    received: list[ChatTextMessage] = []
    chat.on_message(received.append)

    msg = ChatTextMessage(id="m1", from_="provider", ts=1000, text="hi there")
    rtc._dispatch_text(msg)

    assert len(received) == 1
    assert received[0].text == "hi there"
    assert received[0].from_ == "provider"


@pytest.mark.asyncio
async def test_chat_on_image_callback():
    rtc = MockRTC()
    chat = ChatAPI(rtc)

    received: list[ChatImage] = []
    chat.on_image(received.append)

    img = ChatImage(id="i1", from_="provider", ts=1000, mime="image/png", data=b"\x89PNG")
    rtc._dispatch_image(img)

    assert len(received) == 1
    assert received[0].mime == "image/png"


@pytest.mark.asyncio
async def test_chat_history_ephemeral():
    rtc = MockRTC()
    chat = ChatAPI(rtc)

    await chat.send("msg1")
    await chat.send("msg2")

    history = chat.history
    assert len(history) == 2

    rtc._chat_history.clear()
    assert len(chat.history) == 0


@pytest.mark.asyncio
async def test_chat_available():
    rtc = MockRTC()
    chat = ChatAPI(rtc)
    assert chat.available

    rtc.chat_channel.readyState = "closed"
    assert not chat.available

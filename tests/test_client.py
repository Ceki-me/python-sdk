from unittest.mock import MagicMock

import pytest

from ceki_browser import Browser, NavigateResult, QueryResult, Session
from ceki_browser.errors import CekiBrowserError


class MockRTC:
    def __init__(self):
        self._responses: dict[str, dict] = {}
        self._calls: list[tuple[str, dict | None]] = []
        self.cmd_channel = MagicMock()
        self.cmd_channel.readyState = "open"
        self.pc = MagicMock()
        self.pc.connectionState = "connected"

    def set_response(self, method: str, result: dict):
        self._responses[method] = result

    async def send_command(self, method: str, params: dict | None = None, timeout: float = 30.0):
        self._calls.append((method, params))
        if method in self._responses:
            return self._responses[method]
        return {}

    async def close(self):
        pass


class MockTransport:
    def __init__(self):
        self.agent_id = "agent-mock"
        self._event_callback = None
        self._responses: dict[str, dict] = {}
        self._calls: list[tuple[str, dict | None]] = []

    def on_event(self, cb):
        self._event_callback = cb

    def set_response(self, method: str, result: dict):
        self._responses[method] = result

    async def connect(self):
        return {"status": "connected", "agent_id": self.agent_id}

    async def close(self):
        pass

    async def send(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self._calls.append((method, params))
        if method in self._responses:
            return self._responses[method]
        return {}

    async def notify(self, method: str, params: dict | None = None):
        pass

    @property
    def connected(self):
        return True


def make_session_with_mock_rtc(human=None) -> tuple[Session, MockRTC]:
    mt = MockTransport()
    mock_rtc = MockRTC()
    sess = Session(mt, "req-1", "incognito", human=human)
    sess._active = True
    sess._session_id = "sess-1"
    sess._rtc = mock_rtc
    from ceki_browser.chat import ChatAPI
    sess._chat = ChatAPI(mt, "sess-1", None)
    return sess, mock_rtc


@pytest.mark.asyncio
async def test_browser_connect_and_close():
    mt = MockTransport()
    browser = Browser.__new__(Browser)
    browser._transport = mt
    browser._connected = False

    result = await browser.connect()
    assert result["agent_id"] == "agent-mock"
    assert browser.connected

    await browser.close()
    assert not browser.connected


@pytest.mark.asyncio
async def test_session_navigate():
    sess, rtc = make_session_with_mock_rtc()
    rtc.set_response("browser.navigate", {"url": "https://example.com", "title": "Example", "status": 200})

    result = await sess.navigate("https://example.com")
    assert isinstance(result, NavigateResult)
    assert result.url == "https://example.com"
    assert result.title == "Example"


@pytest.mark.asyncio
async def test_session_query():
    sess, rtc = make_session_with_mock_rtc()
    rtc.set_response("browser.query", {"elements": [{"textContent": "Hello World"}]})

    result = await sess.query("h1")
    assert isinstance(result, QueryResult)
    assert result.text == "Hello World"
    assert len(result) == 1


@pytest.mark.asyncio
async def test_session_query_all():
    sess, rtc = make_session_with_mock_rtc()
    rtc.set_response("browser.query_all", {"elements": [
        {"textContent": "Item 1"},
        {"textContent": "Item 2"},
        {"textContent": "Item 3"},
    ]})

    result = await sess.query_all("li")
    assert len(result) == 3


@pytest.mark.asyncio
async def test_session_inactive_raises():
    mt = MockTransport()
    sess = Session(mt, "req-1", "incognito")

    with pytest.raises(CekiBrowserError, match="not active"):
        await sess.navigate("https://example.com")


@pytest.mark.asyncio
async def test_session_click_and_type():
    sess, rtc = make_session_with_mock_rtc()
    rtc.set_response("browser.click", {"clicked": True})
    rtc.set_response("browser.type", {"typed": True})

    await sess.click(selector="#btn")
    await sess.type("#input", "hello")

    assert rtc._calls[0] == ("browser.click", {"selector": "#btn"})
    assert rtc._calls[1] == ("browser.type", {"selector": "#input", "text": "hello", "delay_ms": 0})


@pytest.mark.asyncio
async def test_session_screenshot():
    sess, rtc = make_session_with_mock_rtc()
    rtc.set_response("browser.screenshot", {"data": "base64data", "width": 1920, "height": 1080})

    result = await sess.screenshot()
    assert result.data == "base64data"
    assert result.width == 1920


@pytest.mark.asyncio
async def test_session_end():
    mt = MockTransport()
    mt.set_response("session.end", {"status": "ended"})
    sess = Session(mt, "req-1", "incognito")
    sess._active = True
    sess._session_id = "sess-1"

    await sess.end()
    assert not sess.active


@pytest.mark.asyncio
async def test_session_context_manager():
    mt = MockTransport()
    mt.set_response("session.end", {"status": "ended"})
    sess = Session(mt, "req-1", "incognito")
    sess._active = True

    async with sess:
        assert sess.active
    assert not sess.active

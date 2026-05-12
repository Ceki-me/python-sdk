from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest

from ceki_browser import Browser


@pytest.fixture
def browser():
    client = AsyncMock()
    client._active_browsers = {}

    match = AsyncMock()
    match.session_id = "test-session"
    match.schedule_id = 1
    match.chat_topic_id = None
    match.browser_info = {}
    match.provider_user_id = None

    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        b = Browser(client, match)
    return b


async def test_screenshot_default_returns_dict(browser: Browser):
    cdp_resp = {"data": "AAAA", "width": 100, "height": 200}
    browser.send = AsyncMock(return_value=cdp_resp)
    result = await browser.screenshot()
    assert isinstance(result, dict)
    assert result["data"] == "AAAA"


async def test_screenshot_base64_returns_dict(browser: Browser):
    cdp_resp = {"data": "AAAA"}
    browser.send = AsyncMock(return_value=cdp_resp)
    result = await browser.screenshot(format="base64")
    assert isinstance(result, dict)
    assert result is cdp_resp


async def test_screenshot_png_returns_bytes(browser: Browser):
    raw = b"\x89PNG"
    cdp_resp = {"data": base64.b64encode(raw).decode()}
    browser.send = AsyncMock(return_value=cdp_resp)
    result = await browser.screenshot(format="png")
    assert isinstance(result, bytes)
    assert result == raw


async def test_screenshot_png_empty_data_returns_empty_bytes(browser: Browser):
    cdp_resp = {"data": ""}
    browser.send = AsyncMock(return_value=cdp_resp)
    result = await browser.screenshot(format="png")
    assert result == b""


async def test_screenshot_invalid_format_raises(browser: Browser):
    with pytest.raises(ValueError, match="Unsupported format"):
        await browser.screenshot(format="bogus")

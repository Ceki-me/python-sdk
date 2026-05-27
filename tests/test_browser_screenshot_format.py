from __future__ import annotations

import base64
import logging
from unittest.mock import AsyncMock, call, patch

import pytest

from ceki_sdk import Browser


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


async def test_screenshot_full_page_sends_layout_metrics_and_clip(browser: Browser):
    metrics_resp = {"contentSize": {"width": 1280, "height": 5000}}
    cdp_resp = {"data": "AAAA"}
    browser.send = AsyncMock(side_effect=[metrics_resp, cdp_resp])
    result = await browser.screenshot(full_page=True)
    assert isinstance(result, dict)
    assert browser.send.call_count == 2
    assert browser.send.call_args_list[0] == call({"method": "Page.getLayoutMetrics"})
    capture_call = browser.send.call_args_list[1].args[0]
    assert capture_call["method"] == "Page.captureScreenshot"
    assert capture_call["params"]["captureBeyondViewport"] is True
    assert capture_call["params"]["clip"] == {"x": 0, "y": 0, "width": 1280, "height": 5000, "scale": 1}


async def test_screenshot_full_page_clamps_height(browser: Browser, caplog):
    metrics_resp = {"contentSize": {"width": 1920, "height": 20000}}
    cdp_resp = {"data": "AAAA"}
    browser.send = AsyncMock(side_effect=[metrics_resp, cdp_resp])
    with caplog.at_level(logging.WARNING, logger="ceki_sdk._browser"):
        await browser.screenshot(full_page=True)
    capture_call = browser.send.call_args_list[1].args[0]
    assert capture_call["params"]["clip"]["height"] == 16384
    assert "clamped" in caplog.text


async def test_screenshot_full_page_png_returns_bytes(browser: Browser):
    raw = b"\x89PNG_FULL"
    metrics_resp = {"contentSize": {"width": 800, "height": 3000}}
    cdp_resp = {"data": base64.b64encode(raw).decode()}
    browser.send = AsyncMock(side_effect=[metrics_resp, cdp_resp])
    result = await browser.screenshot(format="png", full_page=True)
    assert isinstance(result, bytes)
    assert result == raw


async def test_screenshot_default_no_full_page(browser: Browser):
    cdp_resp = {"data": "AAAA"}
    browser.send = AsyncMock(return_value=cdp_resp)
    await browser.screenshot()
    assert browser.send.call_count == 1
    sent = browser.send.call_args.args[0]
    assert sent["method"] == "Page.captureScreenshot"
    assert sent.get("params", {}).get("captureBeyondViewport") is None

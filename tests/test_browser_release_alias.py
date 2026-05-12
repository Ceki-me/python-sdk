from __future__ import annotations

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


async def test_release_exists_and_callable(browser: Browser):
    assert hasattr(browser, "release")
    assert callable(browser.release)


async def test_release_delegates_to_close(browser: Browser):
    browser.close = AsyncMock()
    await browser.release()
    browser.close.assert_awaited_once_with(timeout=10.0)


async def test_release_passes_timeout(browser: Browser):
    browser.close = AsyncMock()
    await browser.release(timeout=5.0)
    browser.close.assert_awaited_once_with(timeout=5.0)

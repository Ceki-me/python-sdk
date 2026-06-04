from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


async def test_type_sends_single_typetext_command(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("hi")

    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "hi"
    # humanizer disabled → human is None
    assert type_events[0]["params"]["human"] is None
    # extension owns keymap — no per-char CDP wire from SDK
    assert not [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]
    assert not [s for s in sent if s["method"] == "Input.insertText"]


async def test_type_uppercase_text_passes_through_verbatim(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("Hi")

    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "Hi"
    # extension handles Shift / modifiers — SDK does not emit key events
    assert not [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]


async def test_type_digits_and_punctuation_pass_through_verbatim(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("1!")

    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "1!"
    # extension owns shift-encoding for punctuation
    assert not [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]


async def test_type_non_ascii_is_forwarded_as_typetext(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("ы")

    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "ы"
    # no fallback Input.insertText from SDK — extension handles non-ASCII
    assert not [s for s in sent if s["method"] == "Input.insertText"]

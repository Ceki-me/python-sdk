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


async def test_type_sends_keydown_keyup_per_char(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("hi")

    key_events = [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]
    keydowns = [s for s in key_events if s["params"]["type"] == "keyDown"]
    keyups = [s for s in key_events if s["params"]["type"] == "keyUp"]
    assert len(keydowns) == 2
    assert len(keyups) == 2
    assert keydowns[0]["params"]["key"] == "h"
    assert keydowns[0]["params"]["code"] == "KeyH"
    assert keydowns[1]["params"]["key"] == "i"
    assert keydowns[1]["params"]["code"] == "KeyI"


async def test_type_uppercase_uses_shift(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("Hi")

    key_events = [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]
    # H: shift_down, keyDown(H), keyUp(H), shift_up = 4
    # i: keyDown(i), keyUp(i) = 2
    # Total = 6
    assert len(key_events) == 6
    assert key_events[0]["params"]["key"] == "Shift"
    assert key_events[0]["params"]["type"] == "keyDown"
    assert key_events[1]["params"]["key"] == "H"
    assert key_events[1]["params"]["modifiers"] == 8
    assert key_events[4]["params"]["key"] == "i"


async def test_type_digits_and_punctuation(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("1!")

    key_events = [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]
    # 1: keyDown, keyUp = 2
    # !: shift_down, keyDown, keyUp, shift_up = 4
    assert len(key_events) == 6
    digit_down = key_events[0]
    assert digit_down["params"]["code"] == "Digit1"
    assert digit_down["params"]["text"] == "1"
    excl_down = key_events[3]
    assert excl_down["params"]["text"] == "!"
    assert excl_down["params"]["modifiers"] == 8


async def test_type_non_ascii_falls_back_to_insert_text(browser: Browser):
    sent: list[dict] = []
    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}
    browser.send = fake_send

    await browser.type("ы")

    insert_events = [s for s in sent if s["method"] == "Input.insertText"]
    assert len(insert_events) == 1
    assert insert_events[0]["params"]["text"] == "ы"

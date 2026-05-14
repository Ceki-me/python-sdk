from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ceki_browser import Browser


@pytest.fixture
def browser_humanized():
    client = AsyncMock()
    client._active_browsers = {}

    match = AsyncMock()
    match.session_id = "test-session"
    match.schedule_id = 1
    match.chat_topic_id = None
    match.browser_info = {}
    match.provider_user_id = None

    b = Browser(client, match, human="natural")
    return b


@pytest.fixture
def browser_no_human():
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


async def test_humanizer_on_with_pointer_clicks_before_type(browser_humanized: Browser):
    """humanizer ON + last_pointer set → click() before dispatchKeyEvent."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    browser_humanized.send = fake_send
    browser_humanized._last_pointer = (100, 200)

    await browser_humanized.type("ab")

    mouse_events = [s for s in sent if s["method"] == "Input.dispatchMouseEvent"]
    key_events = [
        s for s in sent
        if s["method"] == "Input.dispatchKeyEvent"
        and s["params"]["type"] == "keyDown"
        and s["params"].get("key") != "Shift"
    ]

    assert len(mouse_events) >= 2, "should have mousePressed + mouseReleased"
    assert any(e["params"]["type"] == "mousePressed" for e in mouse_events)
    assert any(e["params"]["type"] == "mouseReleased" for e in mouse_events)
    assert len(key_events) == 2, "should have per-char keyDown events"

    first_mouse_idx = sent.index(mouse_events[0])
    first_key_idx = sent.index(key_events[0])
    assert first_mouse_idx < first_key_idx, "mouse events must precede key events"


async def test_humanizer_on_no_pointer_no_click(browser_humanized: Browser):
    """humanizer ON + last_pointer is None → no mouse events, just dispatchKeyEvent."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    browser_humanized.send = fake_send
    assert browser_humanized._last_pointer is None

    await browser_humanized.type("x")

    mouse_events = [s for s in sent if s["method"] == "Input.dispatchMouseEvent"]
    key_events = [
        s for s in sent
        if s["method"] == "Input.dispatchKeyEvent"
        and s["params"]["type"] == "keyDown"
        and s["params"].get("key") != "Shift"
    ]

    assert len(mouse_events) == 0, "no mouse events without last_pointer"
    assert len(key_events) >= 1


async def test_humanizer_off_with_pointer_no_click(browser_no_human: Browser):
    """humanizer OFF + last_pointer set → per-char dispatchKeyEvent, no click."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    browser_no_human._last_pointer = (50, 60)
    browser_no_human.send = fake_send

    await browser_no_human.type("hello")

    key_events = [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]
    keydowns = [
        s for s in key_events
        if s["params"]["type"] == "keyDown"
        and s["params"].get("key") != "Shift"
    ]
    assert len(keydowns) == 5, "should have 5 keyDown events for 'hello'"
    assert keydowns[0]["params"]["key"] == "h"
    assert keydowns[4]["params"]["key"] == "o"


async def test_humanizer_off_no_pointer_no_click(browser_no_human: Browser):
    """humanizer OFF + no last_pointer → per-char dispatchKeyEvent, no click."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    assert browser_no_human._last_pointer is None
    browser_no_human.send = fake_send

    await browser_no_human.type("world")

    key_events = [s for s in sent if s["method"] == "Input.dispatchKeyEvent"]
    keydowns = [
        s for s in key_events
        if s["params"]["type"] == "keyDown"
        and s["params"].get("key") != "Shift"
    ]
    assert len(keydowns) == 5, "should have 5 keyDown events for 'world'"
    assert keydowns[0]["params"]["key"] == "w"
    assert keydowns[4]["params"]["key"] == "d"

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ceki_sdk import Browser


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


async def test_humanizer_on_with_pointer_clicks_before_typetext(browser_humanized: Browser):
    """humanizer ON + last_pointer set → SDK pre-focus click() before Ceki.typeText."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    browser_humanized.send = fake_send
    browser_humanized._last_pointer = (100, 200)

    await browser_humanized.type("ab")

    mouse_events = [s for s in sent if s["method"] == "Input.dispatchMouseEvent"]
    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]

    assert len(mouse_events) >= 2, "should have mousePressed + mouseReleased"
    assert any(e["params"]["type"] == "mousePressed" for e in mouse_events)
    assert any(e["params"]["type"] == "mouseReleased" for e in mouse_events)
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "ab"
    assert type_events[0]["params"]["human"] == "natural"

    first_mouse_idx = sent.index(mouse_events[0])
    typetext_idx = sent.index(type_events[0])
    assert first_mouse_idx < typetext_idx, "pre-focus click must precede Ceki.typeText"


async def test_humanizer_on_no_pointer_no_click(browser_humanized: Browser):
    """humanizer ON + last_pointer is None → no pre-focus click, just Ceki.typeText."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    browser_humanized.send = fake_send
    assert browser_humanized._last_pointer is None

    await browser_humanized.type("x")

    mouse_events = [s for s in sent if s["method"] == "Input.dispatchMouseEvent"]
    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]

    assert len(mouse_events) == 0, "no pre-focus click without last_pointer"
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "x"
    assert type_events[0]["params"]["human"] == "natural"


async def test_humanizer_off_with_pointer_no_click(browser_no_human: Browser):
    """humanizer OFF + last_pointer set → no pre-focus click, single Ceki.typeText, human=None."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    browser_no_human._last_pointer = (50, 60)
    browser_no_human.send = fake_send

    await browser_no_human.type("hello")

    mouse_events = [s for s in sent if s["method"] == "Input.dispatchMouseEvent"]
    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]

    assert len(mouse_events) == 0, "no pre-focus click without humanizer"
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "hello"
    assert type_events[0]["params"]["human"] is None


async def test_humanizer_off_no_pointer_no_click(browser_no_human: Browser):
    """humanizer OFF + no last_pointer → single Ceki.typeText, human=None."""
    sent: list[dict] = []

    async def fake_send(cdp, **kw):
        sent.append(cdp)
        return {}

    assert browser_no_human._last_pointer is None
    browser_no_human.send = fake_send

    await browser_no_human.type("world")

    mouse_events = [s for s in sent if s["method"] == "Input.dispatchMouseEvent"]
    type_events = [s for s in sent if s["method"] == "Ceki.typeText"]

    assert len(mouse_events) == 0
    assert len(type_events) == 1
    assert type_events[0]["params"]["text"] == "world"
    assert type_events[0]["params"]["human"] is None

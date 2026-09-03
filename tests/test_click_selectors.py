from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ceki_sdk._browser import Browser
from ceki_sdk.cli import build_parser

# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _make_browser() -> Browser:
    client = AsyncMock()
    client._active_browsers = {}
    client.chat_url = "https://test/chat"
    client.api_key = "test"

    match = AsyncMock()
    match.session_id = "click-1"
    match.schedule_id = 1
    match.chat_topic_id = "t1"
    match.browser_info = {}
    match.provider_user_id = None

    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        return Browser(client, match)


def _route_send(evaluate_value: str | None) -> AsyncMock:
    """Send mock: Runtime.evaluate answers ``evaluate_value``, mouse events return {}."""

    async def _send(cdp, **kw):
        if cdp["method"] == "Runtime.evaluate":
            return {"result": {"value": evaluate_value}}
        return {}

    return AsyncMock(side_effect=_send)


def _cdp_calls(b: Browser) -> list[dict]:
    return [c.args[0] for c in b.send.call_args_list]


def _mouse_calls(b: Browser) -> list[dict]:
    return [c for c in _cdp_calls(b) if c["method"] == "Input.dispatchMouseEvent"]


# ──────────────────────────────────────────────────────────────────────────
# click(selector=...) — resolves via Runtime.evaluate then native click
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_by_selector_resolves_then_clicks():
    b = _make_browser()
    b.send = _route_send(json.dumps({"x": 120, "y": 45}))

    await b.click(selector="button[type=submit]")

    calls = _cdp_calls(b)
    assert calls[0]["method"] == "Runtime.evaluate"
    expr = calls[0]["params"]["expression"]
    assert "document.querySelector" in expr
    assert '"button[type=submit]"' in expr
    assert "getBoundingClientRect" in expr

    mouse = _mouse_calls(b)
    assert len(mouse) == 2
    assert mouse[0]["params"]["x"] == 120
    assert mouse[0]["params"]["type"] == "mousePressed"
    assert mouse[1]["params"]["y"] == 45
    assert b._last_pointer == (120, 45)


@pytest.mark.asyncio
async def test_click_by_selector_not_found_raises():
    b = _make_browser()
    b.send = _route_send(json.dumps({"error": "no element matched selector"}))

    with pytest.raises(ValueError, match="no element found"):
        await b.click(selector="#missing")


# ──────────────────────────────────────────────────────────────────────────
# click(text=...) — visible-text scan then native click
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_by_text_resolves_then_clicks():
    b = _make_browser()
    b.send = _route_send(json.dumps({"x": 88, "y": 320}))

    await b.click(text="Sign Up")

    calls = _cdp_calls(b)
    assert calls[0]["method"] == "Runtime.evaluate"
    expr = calls[0]["params"]["expression"]
    # JS literal for the needle is JSON-escaped and lowercased on the SDK side
    assert '"sign up"' in expr
    assert "textContent" in expr
    assert "scrollIntoView" in expr

    mouse = _mouse_calls(b)
    assert len(mouse) == 2
    assert mouse[0]["params"]["x"] == 88
    assert mouse[1]["params"]["y"] == 320


@pytest.mark.asyncio
async def test_click_by_text_not_found_raises():
    b = _make_browser()
    b.send = _route_send(json.dumps({"error": "no visible element with text"}))

    with pytest.raises(ValueError, match="no element found"):
        await b.click(text="No Such Button")


# ──────────────────────────────────────────────────────────────────────────
# Backward compatibility — click(x, y) untouched
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_xy_still_native_only():
    b = _make_browser()
    b.send = AsyncMock(return_value={})

    await b.click(10, 20)

    methods = [c["method"] for c in _cdp_calls(b)]
    assert "Runtime.evaluate" not in methods
    assert methods.count("Input.dispatchMouseEvent") == 2
    mouse = _mouse_calls(b)
    assert mouse[0]["params"]["x"] == 10
    assert mouse[1]["params"]["y"] == 20
    assert b._last_pointer == (10, 20)


# ──────────────────────────────────────────────────────────────────────────
# Argument validation
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_no_mode_raises():
    b = _make_browser()
    with pytest.raises(TypeError):
        await b.click()


@pytest.mark.asyncio
async def test_click_selector_and_text_conflict():
    b = _make_browser()
    with pytest.raises((TypeError, ValueError)):
        await b.click(selector="#a", text="b")


@pytest.mark.asyncio
async def test_click_selector_and_coordinates_conflict():
    b = _make_browser()
    with pytest.raises(ValueError):
        await b.click(10, 20, selector="#a")


# ──────────────────────────────────────────────────────────────────────────
# CLI parser — --selector / --text / legacy x y
# ──────────────────────────────────────────────────────────────────────────


def test_parser_click_selector():
    parser = build_parser()
    args = parser.parse_args(["click", "ses-1", "--selector", "button[type=submit]"])
    assert args.command == "click"
    assert args.session_id == "ses-1"
    assert args.selector == "button[type=submit]"
    assert args.text is None
    assert args.x is None
    assert args.y is None


def test_parser_click_text():
    parser = build_parser()
    args = parser.parse_args(["click", "ses-1", "--text", "Sign Up"])
    assert args.command == "click"
    assert args.text == "Sign Up"
    assert args.selector is None
    assert args.x is None
    assert args.y is None


def test_parser_click_coords_legacy():
    parser = build_parser()
    args = parser.parse_args(["click", "ses-1", "100", "200"])
    assert args.x == 100
    assert args.y == 200
    assert args.selector is None
    assert args.text is None

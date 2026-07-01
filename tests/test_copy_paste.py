"""Regression guards for ``Browser.copy()`` and ``Browser.paste(selector, text)``.

Both methods are pure CDP passthrough (``Runtime.evaluate`` +
``Input.insertText``) — nothing on the extension / relay side changes. These
tests pin the wire shape so we don't accidentally drift into
``navigator.clipboard`` or the wrong CDP verb.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from ceki_sdk._browser import Browser


def _make_browser() -> Browser:
    client = AsyncMock()
    client._active_browsers = {}
    client.chat_url = "https://test/chat"
    client.api_key = "test"

    match = AsyncMock()
    match.session_id = "copypaste-1"
    match.schedule_id = 1
    match.chat_topic_id = "t1"
    match.browser_info = {}
    match.provider_user_id = None

    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        return Browser(client, match)


# ──────────────────────────────────────────────────────────────────────────
# copy()
# ──────────────────────────────────────────────────────────────────────────


async def test_copy_sends_getselection_and_returns_value():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {"value": "hello world"}})

    got = await b.copy()

    assert got == "hello world"
    assert b.send.await_count == 1
    call = b.send.await_args_list[0][0][0]
    assert call["method"] == "Runtime.evaluate"
    assert call["params"]["expression"] == "window.getSelection().toString()"
    assert call["params"]["returnByValue"] is True


async def test_copy_returns_empty_when_selection_missing():
    """Empty selection: CDP returns ``{"result": {}}`` (no ``value`` key).

    We must not raise KeyError — the SDK guards with ``.get(...) or ""``.
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    assert await b.copy() == ""


async def test_copy_returns_empty_when_result_missing():
    b = _make_browser()
    b.send = AsyncMock(return_value={})

    assert await b.copy() == ""


async def test_copy_returns_empty_string_verbatim_when_value_is_empty_string():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {"value": ""}})

    assert await b.copy() == ""


# ──────────────────────────────────────────────────────────────────────────
# paste(selector, text)
# ──────────────────────────────────────────────────────────────────────────


async def test_paste_sends_focus_then_insertText_in_order():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    await b.paste("#foo", "bar")

    assert b.send.await_count == 2

    first = b.send.await_args_list[0][0][0]
    assert first["method"] == "Runtime.evaluate"
    assert first["params"]["expression"] == 'document.querySelector("#foo").focus()'

    second = b.send.await_args_list[1][0][0]
    assert second["method"] == "Input.insertText"
    assert second["params"] == {"text": "bar"}


async def test_paste_json_escapes_weird_selector():
    """Selector with double quotes, single quotes, backslashes must survive.

    We MUST NOT string-concat into the JS expression — the selector goes through
    ``json.dumps`` so anything that fits in a JSON string fits in a JS string.
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    nasty = " #foo[data-x='\"a\\\"b\"'] "
    await b.paste(nasty, "x")

    first = b.send.await_args_list[0][0][0]
    expected_expr = f"document.querySelector({json.dumps(nasty)}).focus()"
    assert first["params"]["expression"] == expected_expr


async def test_paste_selector_with_newlines_and_unicode():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    weird = "#foo\n[data-x='ñü']`"
    await b.paste(weird, "y")

    first = b.send.await_args_list[0][0][0]
    # json.dumps escapes newline -> \n and backticks pass through fine
    assert first["params"]["expression"] == (
        f"document.querySelector({json.dumps(weird)}).focus()"
    )


async def test_paste_text_passes_through_verbatim_to_insertText():
    """text goes to Input.insertText.text as-is — no re-escaping required on the
    CDP side. This includes newlines, quotes, and unicode."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    payload = "text with \n newlines and 'quotes' and unicode ñ"
    await b.paste("#foo", payload)

    second = b.send.await_args_list[1][0][0]
    assert second["method"] == "Input.insertText"
    assert second["params"]["text"] == payload


async def test_paste_empty_text_still_sends_insertText():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    await b.paste("#foo", "")

    assert b.send.await_count == 2
    second = b.send.await_args_list[1][0][0]
    assert second == {"method": "Input.insertText", "params": {"text": ""}}

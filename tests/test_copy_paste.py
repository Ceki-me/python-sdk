"""Regression guards for ``Browser.copy()`` and ``Browser.paste(selector, text)``.

Both methods drive the *OS clipboard* via synthetic Ctrl+C / Ctrl+V hotkeys
(``Input.dispatchKeyEvent`` with ``modifiers=2``). ``paste`` seeds arbitrary
text by staging it through an offscreen ``<textarea>`` before the Ctrl+C.
Nothing on the extension / relay side changes. These tests pin the wire shape
so we don't accidentally regress to ``Input.insertText`` (which is direct DOM
insertion, not the OS clipboard) or drift into ``navigator.clipboard``.
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


def _assert_ctrl_hotkey(call: dict, *, typ: str, key: str, code: str) -> None:
    assert call["method"] == "Input.dispatchKeyEvent"
    params = call["params"]
    assert params["type"] == typ
    assert params["modifiers"] == 2
    assert params["key"] == key
    assert params["code"] == code
    vk = ord(key.upper())
    assert params["windowsVirtualKeyCode"] == vk
    assert params["nativeVirtualKeyCode"] == vk


# ──────────────────────────────────────────────────────────────────────────
# copy()
# ──────────────────────────────────────────────────────────────────────────


async def test_copy_reads_selection_then_dispatches_ctrl_c():
    """copy() reads the selection via Runtime.evaluate then fires Ctrl+C.

    Wire order:
      1. Runtime.evaluate — window.getSelection().toString()
      2. Input.dispatchKeyEvent(type=keyDown, key='c', code='KeyC', modifiers=2)
      3. Input.dispatchKeyEvent(type=keyUp,   key='c', code='KeyC', modifiers=2)
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {"value": "hello world"}})

    got = await b.copy()

    assert got == "hello world"
    assert b.send.await_count == 3

    first = b.send.await_args_list[0][0][0]
    assert first["method"] == "Runtime.evaluate"
    assert first["params"]["expression"] == "window.getSelection().toString()"
    assert first["params"]["returnByValue"] is True

    _assert_ctrl_hotkey(b.send.await_args_list[1][0][0], typ="keyDown", key="c", code="KeyC")
    _assert_ctrl_hotkey(b.send.await_args_list[2][0][0], typ="keyUp", key="c", code="KeyC")


async def test_copy_returns_empty_when_selection_missing():
    """Empty selection: CDP returns ``{"result": {}}`` (no ``value`` key).

    We must not raise KeyError — the SDK guards with ``.get(...) or ""``.
    Ctrl+C still fires (it's cheap and keeps the OS clipboard state
    consistent with what the caller sees).
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    assert await b.copy() == ""
    # 1 Runtime.evaluate + 2 dispatchKeyEvent
    assert b.send.await_count == 3


async def test_copy_returns_empty_when_result_missing():
    b = _make_browser()
    b.send = AsyncMock(return_value={})

    assert await b.copy() == ""
    assert b.send.await_count == 3


async def test_copy_returns_empty_string_verbatim_when_value_is_empty_string():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {"value": ""}})

    assert await b.copy() == ""
    assert b.send.await_count == 3


async def test_copy_never_calls_insertText():
    """Guard against regression to the 4091 direct-insertion path."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {"value": "x"}})
    await b.copy()
    for call in b.send.await_args_list:
        assert call[0][0]["method"] != "Input.insertText"


# ──────────────────────────────────────────────────────────────────────────
# paste(selector, text)
# ──────────────────────────────────────────────────────────────────────────


async def test_paste_wire_sequence():
    """paste() runs: seed textarea -> Ctrl+C -> cleanup+focus -> Ctrl+V.

    Exactly 6 CDP calls:
      1. Runtime.evaluate — build offscreen <textarea>, set .value, focus+select
      2. Input.dispatchKeyEvent keyDown 'c'
      3. Input.dispatchKeyEvent keyUp   'c'
      4. Runtime.evaluate — remove temp, querySelector(...).focus()
      5. Input.dispatchKeyEvent keyDown 'v'
      6. Input.dispatchKeyEvent keyUp   'v'
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    await b.paste("#foo", "bar")

    assert b.send.await_count == 6
    calls = [c[0][0] for c in b.send.await_args_list]

    # 1: seed textarea
    assert calls[0]["method"] == "Runtime.evaluate"
    seed_expr = calls[0]["params"]["expression"]
    assert "document.createElement('textarea')" in seed_expr
    assert "position:fixed;left:-9999px" in seed_expr
    # text is JSON-escaped ("bar" -> "\"bar\"")
    assert '="bar"' in seed_expr.replace(" ", "")  # tolerant of whitespace

    # 2-3: Ctrl+C
    _assert_ctrl_hotkey(calls[1], typ="keyDown", key="c", code="KeyC")
    _assert_ctrl_hotkey(calls[2], typ="keyUp", key="c", code="KeyC")

    # 4: cleanup + focus target
    assert calls[3]["method"] == "Runtime.evaluate"
    cf_expr = calls[3]["params"]["expression"]
    assert "__ceki_paste_tmp__" in cf_expr
    assert 'document.querySelector("#foo")' in cf_expr
    assert ".focus()" in cf_expr

    # 5-6: Ctrl+V
    _assert_ctrl_hotkey(calls[4], typ="keyDown", key="v", code="KeyV")
    _assert_ctrl_hotkey(calls[5], typ="keyUp", key="v", code="KeyV")


async def test_paste_never_calls_insertText():
    """Guard against regression to the 4091 direct-insertion path."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})
    await b.paste("#foo", "bar")
    for c in b.send.await_args_list:
        assert c[0][0]["method"] != "Input.insertText"


async def test_paste_json_escapes_weird_selector():
    """Selector with double quotes, single quotes, backslashes must survive.

    The selector goes through ``json.dumps`` so anything that fits in a JSON
    string fits in a JS string. We check the *cleanup+focus* Runtime.evaluate.
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    nasty = " #foo[data-x='\"a\\\"b\"'] "
    await b.paste(nasty, "x")

    cf_expr = b.send.await_args_list[3][0][0]["params"]["expression"]
    assert f"document.querySelector({json.dumps(nasty)})" in cf_expr


async def test_paste_selector_with_newlines_and_unicode():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    weird = "#foo\n[data-x='ñü']`"
    await b.paste(weird, "y")

    cf_expr = b.send.await_args_list[3][0][0]["params"]["expression"]
    assert f"document.querySelector({json.dumps(weird)})" in cf_expr


async def test_paste_json_escapes_text_with_quotes_newlines_unicode_backticks():
    """text is interpolated into a JS expression — it MUST be JSON-escaped."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    payload = 'text with \n newlines, "quotes", \\backslash, `backtick`, ñü'
    await b.paste("#foo", payload)

    seed_expr = b.send.await_args_list[0][0][0]["params"]["expression"]
    # The raw text must NOT appear un-escaped
    assert payload not in seed_expr
    # The JSON-escaped form MUST appear
    assert json.dumps(payload) in seed_expr


async def test_paste_empty_text_still_seeds_and_fires_ctrl_v():
    """Empty text is still a valid clipboard payload; the whole hotkey dance
    still runs (6 CDP calls) and the seed literal is an empty JSON string."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    await b.paste("#foo", "")

    assert b.send.await_count == 6
    seed_expr = b.send.await_args_list[0][0][0]["params"]["expression"]
    assert '=""' in seed_expr.replace(" ", "")

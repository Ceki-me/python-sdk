"""Anti-detect probabilistic paste path in ``Browser.type()`` (task 4109).

``type(text, selector=...)`` gets two gates before it routes through the
real-clipboard Ctrl+V path from task 4098:

  1. ``selector`` is not ``None`` (we need a focus target for the OS paste).
  2. ``len(text) > TYPE_PASTE_MIN_CHARS`` (short text has no rhythm signature).
  3. ``random.random() < TYPE_PASTE_PROBABILITY``.

When any gate fails, the existing per-key ``Ceki.typeText`` path is used
verbatim. These tests pin every gate, prove the shared hotkey helper is the
same wire shape as :func:`Browser.paste`, and guard the module constants
against future magic-number drift.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from ceki_sdk import _browser
from ceki_sdk._browser import Browser


def _make_browser() -> Browser:
    client = AsyncMock()
    client._active_browsers = {}
    client.chat_url = "https://test/chat"
    client.api_key = "test"

    match = AsyncMock()
    match.session_id = "typepaste-1"
    match.schedule_id = 1
    match.chat_topic_id = "t1"
    match.browser_info = {}
    match.provider_user_id = None

    # CEKI_HUMAN_DISABLE=1 so the humanizer does not fire click() calls that
    # would add noise to send.await_count assertions.
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
# Module constants
# ──────────────────────────────────────────────────────────────────────────


def test_constants_at_module_scope():
    """The threshold and probability MUST live at module scope so tests can
    pin them and future tuning does not leave magic numbers in two places
    (``type()`` body + ``_hotkey_paste_into`` doc string, etc.)."""
    assert _browser.TYPE_PASTE_MIN_CHARS == 500
    assert _browser.TYPE_PASTE_PROBABILITY == 0.625


# ──────────────────────────────────────────────────────────────────────────
# Gate A — no selector → NEVER paste path, even for long text and forced dice
# ──────────────────────────────────────────────────────────────────────────


async def test_type_no_selector_never_paste_even_when_long_and_dice_low():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    long_text = "X" * 5000
    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type(long_text)

    # One Ceki.typeText call — nothing else.
    assert b.send.await_count == 1
    call = b.send.await_args_list[0][0][0]
    assert call["method"] == "Ceki.typeText"
    assert call["params"]["text"] == long_text
    assert "selector" not in call["params"]


# ──────────────────────────────────────────────────────────────────────────
# Gate B — text length
# ──────────────────────────────────────────────────────────────────────────


async def test_type_short_text_with_selector_stays_per_key():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type("a" * 100, selector="#in")

    assert b.send.await_count == 1
    call = b.send.await_args_list[0][0][0]
    assert call["method"] == "Ceki.typeText"
    assert call["params"]["selector"] == "#in"


async def test_type_at_threshold_exact_500_stays_per_key():
    """Boundary: ``> 500`` means 500 exactly still goes per-key."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type("a" * 500, selector="#in")

    assert b.send.await_count == 1
    assert b.send.await_args_list[0][0][0]["method"] == "Ceki.typeText"


async def test_type_empty_string_with_selector_never_paste():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type("", selector="#in")

    assert b.send.await_count == 1
    assert b.send.await_args_list[0][0][0]["method"] == "Ceki.typeText"


async def test_type_whitespace_only_and_single_char_never_paste():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type("   ", selector="#in")
        await b.type("x", selector="#in")

    # Two per-key calls, nothing else.
    assert b.send.await_count == 2
    for c in b.send.await_args_list:
        assert c[0][0]["method"] == "Ceki.typeText"


# ──────────────────────────────────────────────────────────────────────────
# Gate C — probability
# ──────────────────────────────────────────────────────────────────────────


async def test_type_long_with_selector_dice_above_threshold_stays_per_key():
    """random() >= 0.625 falls through to Ceki.typeText."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    long_text = "X" * 600
    with patch("ceki_sdk._browser.random.random", return_value=0.9):
        await b.type(long_text, selector="#in")

    assert b.send.await_count == 1
    assert b.send.await_args_list[0][0][0]["method"] == "Ceki.typeText"


async def test_type_long_with_selector_dice_at_boundary_stays_per_key():
    """random() == 0.625 (not strictly less than) falls through."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    with patch("ceki_sdk._browser.random.random", return_value=0.625):
        await b.type("X" * 600, selector="#in")

    assert b.send.await_count == 1
    assert b.send.await_args_list[0][0][0]["method"] == "Ceki.typeText"


# ──────────────────────────────────────────────────────────────────────────
# Paste path fires: exact 6-CDP-call sequence, no Ceki.typeText
# ──────────────────────────────────────────────────────────────────────────


async def test_type_long_with_selector_dice_low_fires_paste_sequence():
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    long_text = "X" * 600
    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type(long_text, selector="#in")

    # Exactly the 6-call clipboard-hotkey sequence from task 4098:
    #   1 seed textarea, 2-3 Ctrl+C, 4 cleanup+focus, 5-6 Ctrl+V.
    assert b.send.await_count == 6
    calls = [c[0][0] for c in b.send.await_args_list]

    # No Ceki.typeText anywhere — the paste path replaces it entirely.
    for c in calls:
        assert c["method"] != "Ceki.typeText"

    assert calls[0]["method"] == "Runtime.evaluate"
    seed_expr = calls[0]["params"]["expression"]
    assert "document.createElement('textarea')" in seed_expr
    # Text JSON-escaped into the seed.
    assert json.dumps(long_text) in seed_expr

    _assert_ctrl_hotkey(calls[1], typ="keyDown", key="c", code="KeyC")
    _assert_ctrl_hotkey(calls[2], typ="keyUp", key="c", code="KeyC")

    assert calls[3]["method"] == "Runtime.evaluate"
    cf_expr = calls[3]["params"]["expression"]
    assert "__ceki_paste_tmp__" in cf_expr
    assert 'document.querySelector("#in")' in cf_expr
    assert ".focus()" in cf_expr

    _assert_ctrl_hotkey(calls[4], typ="keyDown", key="v", code="KeyV")
    _assert_ctrl_hotkey(calls[5], typ="keyUp", key="v", code="KeyV")


async def test_type_paste_path_json_escapes_weird_selector_and_text():
    """Selector + text both go through json.dumps in the shared helper."""
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    nasty_selector = " #in[data-x='\"a\\\"b\"'] "
    nasty_text = (
        'X' * 600
        + '\n"quotes",\n`backticks`,\\backslash,ñü'
    )

    with patch("ceki_sdk._browser.random.random", return_value=0.0):
        await b.type(nasty_text, selector=nasty_selector)

    calls = [c[0][0] for c in b.send.await_args_list]

    seed_expr = calls[0]["params"]["expression"]
    # Raw text with un-escaped newline must NOT appear un-escaped.
    assert nasty_text not in seed_expr
    assert json.dumps(nasty_text) in seed_expr

    cf_expr = calls[3]["params"]["expression"]
    assert f"document.querySelector({json.dumps(nasty_selector)})" in cf_expr


# ──────────────────────────────────────────────────────────────────────────
# paste() still works — regression guard on the 4098 wire shape
# ──────────────────────────────────────────────────────────────────────────


async def test_paste_public_still_emits_the_same_6_call_sequence():
    """After refactor into _hotkey_paste_into, paste() must still be
    identical on the wire to task 4098 — same 6 CDP calls, same order.
    """
    b = _make_browser()
    b.send = AsyncMock(return_value={"result": {}})

    await b.paste("#foo", "bar")

    assert b.send.await_count == 6
    calls = [c[0][0] for c in b.send.await_args_list]

    assert calls[0]["method"] == "Runtime.evaluate"
    assert "document.createElement('textarea')" in calls[0]["params"]["expression"]
    _assert_ctrl_hotkey(calls[1], typ="keyDown", key="c", code="KeyC")
    _assert_ctrl_hotkey(calls[2], typ="keyUp", key="c", code="KeyC")
    assert calls[3]["method"] == "Runtime.evaluate"
    assert 'document.querySelector("#foo")' in calls[3]["params"]["expression"]
    _assert_ctrl_hotkey(calls[4], typ="keyDown", key="v", code="KeyV")
    _assert_ctrl_hotkey(calls[5], typ="keyUp", key="v", code="KeyV")


# ──────────────────────────────────────────────────────────────────────────
# Statistical sanity — dice distribution matches the constant
# ──────────────────────────────────────────────────────────────────────────


async def test_statistical_gate_matches_probability_constant():
    """Feed a deterministic cycle of ``random()`` values through 200 calls,
    count paste-path invocations, confirm they match the ``< 0.625`` count.

    Belt-and-braces guard that the comparison is strict ``<`` (not ``<=``)
    and that we did not accidentally invert the condition.
    """
    # 200 values in [0, 1). 125 are < 0.625, 75 are >= 0.625.
    values = [i / 200 for i in range(200)]
    expected_paste = sum(1 for v in values if v < _browser.TYPE_PASTE_PROBABILITY)
    assert expected_paste == 125  # sanity check on the fixture itself

    paste_hits = 0
    per_key_hits = 0

    long_text = "X" * 600

    it = iter(values)
    with patch("ceki_sdk._browser.random.random", side_effect=lambda: next(it)):
        for _ in range(200):
            b = _make_browser()
            b.send = AsyncMock(return_value={"result": {}})
            await b.type(long_text, selector="#in")
            n_calls = b.send.await_count
            if n_calls == 6:
                paste_hits += 1
            elif n_calls == 1:
                per_key_hits += 1
            else:
                raise AssertionError(f"unexpected send count {n_calls}")

    assert paste_hits == expected_paste
    assert per_key_hits == 200 - expected_paste

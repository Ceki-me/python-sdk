"""Tests for Browser humanization integration."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_sdk._browser import Browser, _resolve_human
from ceki_sdk.humanize import Humanizer, HumanProfile


def _make_browser(human="natural"):
    """Create a Browser with mocked internals."""
    client = MagicMock()
    client._ws_send = AsyncMock()
    match = MagicMock()
    match.session_id = "test-session"
    match.schedule_id = 1
    match.browser_info = {}
    match.provider_user_id = None
    match.chat_topic_id = None
    b = Browser(client, match, human=human)
    b._ended = asyncio.Event()
    return b


class TestResolveHuman:
    def test_none_returns_none(self):
        assert _resolve_human(None) is None

    def test_string_preset(self):
        h = _resolve_human("natural")
        assert isinstance(h, Humanizer)
        assert h.profile.name == "natural"

    def test_careful_preset(self):
        h = _resolve_human("careful")
        assert isinstance(h, Humanizer)
        assert h.profile.name == "careful"

    def test_dict_profile(self):
        h = _resolve_human({"typing": {"wpm": 130}})
        assert isinstance(h, Humanizer)

    def test_human_profile_instance(self):
        p = HumanProfile.load_preset("natural")
        h = _resolve_human(p)
        assert h.profile is p

    def test_disable_env(self, monkeypatch):
        monkeypatch.setenv("CEKI_HUMAN_DISABLE", "1")
        assert _resolve_human("natural") is None


class TestBrowserHumanNone:
    """human=None means zero overhead."""

    @pytest.mark.asyncio
    async def test_type_sends_single_typetext_with_human_none(self):
        b = _make_browser(human=None)
        b.send = AsyncMock(return_value={})
        await b.type("hello")
        type_calls = [
            c for c in b.send.call_args_list
            if c[0][0].get("method") == "Ceki.typeText"
        ]
        assert len(type_calls) == 1
        assert type_calls[0][0][0]["params"]["text"] == "hello"
        assert type_calls[0][0][0]["params"]["human"] is None

    @pytest.mark.asyncio
    async def test_click_no_sleep(self):
        b = _make_browser(human=None)
        b.send = AsyncMock(return_value={})
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await b.click(100, 200)
            mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_navigate(self):
        b = _make_browser(human=None)
        b.send = AsyncMock(return_value={"frameId": "123"})
        result = await b.navigate("https://example.com")
        assert result == {"frameId": "123"}


class TestBrowserHumanNatural:
    """human="natural" adds delays."""

    @pytest.mark.asyncio
    async def test_type_sends_single_typetext_with_human_natural(self):
        b = _make_browser(human="natural")
        b.send = AsyncMock(return_value={})
        await b.type("abc")
        type_calls = [
            c for c in b.send.call_args_list
            if c[0][0].get("method") == "Ceki.typeText"
        ]
        assert len(type_calls) == 1
        assert type_calls[0][0][0]["params"]["text"] == "abc"
        assert type_calls[0][0][0]["params"]["human"] == "natural"

    @pytest.mark.asyncio
    async def test_click_timing_variance(self):
        """100 clicks should have non-constant timing (std > 0)."""
        b = _make_browser(human="natural")
        b.send = AsyncMock(return_value={})
        import time
        times = []
        for _ in range(20):
            t0 = time.monotonic()
            await b.click(100, 200)
            times.append(time.monotonic() - t0)
        deltas = [abs(times[i+1] - times[i]) for i in range(len(times)-1)]
        assert max(deltas) > 0.001, "Timings should vary with human profile"


class TestSetHuman:
    def test_set_human_returns_previous(self):
        b = _make_browser(human="natural")
        prev = b.set_human("careful")
        assert prev is not None
        assert prev.name == "natural"
        assert b._humanizer.profile.name == "careful"

    def test_set_human_none_disables(self):
        b = _make_browser(human="natural")
        prev = b.set_human(None)
        assert prev is not None
        assert b._humanizer is None

    def test_set_human_from_none(self):
        b = _make_browser(human=None)
        prev = b.set_human("natural")
        assert prev is None
        assert b._humanizer is not None

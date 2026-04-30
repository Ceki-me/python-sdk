from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from ceki_browser.humanize import HumanProfile, Humanizer


class TestHumanProfile:
    def test_load_preset_natural(self):
        p = HumanProfile.load_preset("natural")
        assert p.name == "natural"
        assert p.raw["typing"]["wpm"] == 110

    def test_load_preset_careful(self):
        p = HumanProfile.load_preset("careful")
        assert p.name == "careful"
        assert p.raw["typing"]["wpm"] == 80

    def test_from_dict_roundtrip(self):
        p = HumanProfile.load_preset("natural")
        d = p.to_dict()
        p2 = HumanProfile.from_dict(d)
        assert p2.to_dict() == d

    def test_json_roundtrip(self):
        p = HumanProfile.load_preset("natural")
        j = p.to_json()
        d = json.loads(j)
        p2 = HumanProfile.from_dict(d)
        assert p2.to_dict() == p.to_dict()

    def test_get_range(self):
        p = HumanProfile.load_preset("natural")
        lo, hi = p.get_range("click", "pre")
        assert lo == 80
        assert hi == 350

    def test_get_range_missing(self):
        p = HumanProfile.from_dict({"name": "empty"})
        lo, hi = p.get_range("unknown_action", "pre")
        assert lo == 0
        assert hi == 0

    def test_typing_interval(self):
        p = HumanProfile.load_preset("natural")
        interval = p.typing_interval()
        expected = 60_000 / (110 * 5)
        assert abs(interval - expected) < 0.01

    def test_load_preset_not_found(self):
        with pytest.raises(FileNotFoundError):
            HumanProfile.load_preset("nonexistent")

    def test_from_dict_custom(self):
        p = HumanProfile.from_dict({"typing": {"wpm": 200}})
        assert p.name == "custom"
        assert p.raw["typing"]["wpm"] == 200


class TestHumanizer:
    @pytest.mark.asyncio
    async def test_none_profile_zero_overhead(self):
        h = Humanizer(None)
        import time
        start = time.monotonic()
        await h.before("click")
        await h.after("click")
        elapsed = time.monotonic() - start
        assert elapsed < 0.01

    @pytest.mark.asyncio
    async def test_none_humanize_text_no_delay(self):
        h = Humanizer(None)
        chars = []
        async for ch, delay in h.humanize_text("hello"):
            chars.append((ch, delay))
        assert len(chars) == 5
        assert all(d == 0.0 for _, d in chars)

    @pytest.mark.asyncio
    async def test_humanize_text_jitter_not_constant(self):
        p = HumanProfile.from_dict({"typing": {"wpm": 110, "jitter": 0.35}, "rng_seed": 42})
        h = Humanizer(p)
        delays = []
        async for _, delay in h.humanize_text("abcdefghij"):
            delays.append(delay)
        assert len(set(round(d, 2) for d in delays)) > 1, "Delays should not all be the same"

    @pytest.mark.asyncio
    async def test_humanize_text_min_clamp(self):
        p = HumanProfile.from_dict({"typing": {"wpm": 110, "jitter": 0.35}, "rng_seed": 42})
        h = Humanizer(p)
        async for _, delay in h.humanize_text("abcdefghijklmnop"):
            assert delay >= 20.0

    @pytest.mark.asyncio
    async def test_before_after_with_zero_range(self):
        p = HumanProfile.from_dict({
            "pre_action_ms": {"screenshot": [0, 0]},
            "post_action_ms": {"screenshot": [0, 0]},
        })
        h = Humanizer(p)
        import time
        start = time.monotonic()
        await h.before("screenshot")
        await h.after("screenshot")
        elapsed = time.monotonic() - start
        assert elapsed < 0.01


class TestSetHuman:
    def test_set_human_returns_previous(self):
        from ceki_browser.session import _resolve_human_profile

        p1 = _resolve_human_profile("natural")
        assert p1 is not None
        assert p1.name == "natural"

        p2 = _resolve_human_profile("careful")
        assert p2 is not None
        assert p2.name == "careful"

        p3 = _resolve_human_profile(None)
        assert p3 is None

    def test_resolve_dict(self):
        from ceki_browser.session import _resolve_human_profile

        p = _resolve_human_profile({"typing": {"wpm": 200}})
        assert p.raw["typing"]["wpm"] == 200

    def test_resolve_human_profile_object(self):
        from ceki_browser.session import _resolve_human_profile

        orig = HumanProfile.load_preset("natural")
        p = _resolve_human_profile(orig)
        assert p is orig

    def test_disable_env(self, monkeypatch):
        from ceki_browser.session import _resolve_human_profile

        monkeypatch.setenv("CEKI_HUMAN_DISABLE", "1")
        p = _resolve_human_profile("natural")
        assert p is None

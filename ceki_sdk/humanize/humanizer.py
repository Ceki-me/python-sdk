from __future__ import annotations

import asyncio
import random
from typing import AsyncIterator

from .profile import HumanProfile


class Humanizer:
    def __init__(self, profile: HumanProfile | None):
        self.profile = profile
        self._rng = random.Random()
        if profile and profile.raw.get("rng_seed") is not None:
            self._rng = random.Random(profile.raw["rng_seed"])

    async def before(self, action: str) -> None:
        if not self.profile:
            return
        lo, hi = self.profile.get_range(action, "pre")
        if lo == 0 and hi == 0:
            return
        delay = self._rng.uniform(lo, hi)
        await asyncio.sleep(delay / 1000)

    async def after(self, action: str) -> None:
        if not self.profile:
            return
        lo, hi = self.profile.get_range(action, "post")
        if lo == 0 and hi == 0:
            return
        delay = self._rng.uniform(lo, hi)
        await asyncio.sleep(delay / 1000)

    async def humanize_text(self, text: str) -> AsyncIterator[tuple[str, float]]:
        if not self.profile:
            for ch in text:
                yield ch, 0.0
            return
        typing = self.profile.raw.get("typing", {})
        wpm = typing.get("wpm", 110)
        jitter = typing.get("jitter", 0.35)
        think_prob = typing.get("thinking_pause_prob", 0.0)
        think_ms = typing.get("thinking_pause_ms", [300, 1200])

        mean_interval = 60_000 / (wpm * 5)  # ms per char

        for ch in text:
            sigma = mean_interval * jitter
            delay = self._rng.gauss(mean_interval, sigma)
            delay = max(delay, 20.0)  # clamp min 20ms

            if think_prob > 0 and self._rng.random() < think_prob:
                delay += self._rng.uniform(think_ms[0], think_ms[1])

            yield ch, delay

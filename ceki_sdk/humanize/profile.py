from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "name": "custom",
    "typing": {
        "wpm": 110,
        "jitter": 0.35,
        "thinking_pause_prob": 0.012,
        "thinking_pause_ms": [300, 1200],
        "typo_prob": 0.0,
    },
    "pre_action_ms": {
        "click": [80, 350],
        "type": [120, 500],
        "scroll": [50, 250],
        "navigate": [0, 0],
        "screenshot": [0, 0],
    },
    "post_action_ms": {
        "click": [150, 800],
        "type": [150, 800],
        "scroll": [200, 900],
        "navigate": [400, 1800],
        "screenshot": [0, 0],
    },
    "mouse": {
        "move_before_click": False,
        "trajectory": "off",
    },
    "rng_seed": None,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base, returning a new dict."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class HumanProfile:
    name: str
    raw: dict

    @classmethod
    def from_dict(cls, d: dict) -> HumanProfile:
        merged = _deep_merge(DEFAULTS, d)
        name = d.get("name", "custom")
        merged["name"] = name
        return cls(name=name, raw=merged)

    @classmethod
    def load(cls, path: str | Path) -> HumanProfile:
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def load_preset(cls, name: str) -> HumanProfile:
        preset_dir = Path(__file__).parent / "profiles"
        path = preset_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Preset '{name}' not found at {path}")
        return cls.load(path)

    def to_dict(self) -> dict:
        return copy.deepcopy(self.raw)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.raw, indent=indent)

    def get_range(self, action: str, phase: str) -> tuple[int, int]:
        key = f"{phase}_action_ms"
        mapping = self.raw.get(key, {})
        pair = mapping.get(action, (0, 0))
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            return (pair[0], pair[1])
        return (0, 0)

    def typing_interval(self) -> float:
        wpm = self.raw.get("typing", {}).get("wpm", 110)
        return 60_000 / (wpm * 5)

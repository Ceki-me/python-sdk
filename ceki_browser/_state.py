from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STATE_DIR = Path.home() / ".ceki" / "sessions"


def _ensure_dir() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_session(sid: str) -> dict[str, Any] | None:
    path = _STATE_DIR / f"{sid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_session(sid: str, data: dict[str, Any]) -> None:
    _ensure_dir()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _STATE_DIR / f"{sid}.json"
    path.write_text(json.dumps(data))


def delete_session(sid: str) -> None:
    path = _STATE_DIR / f"{sid}.json"
    path.unlink(missing_ok=True)


def get_last_seen_ts(sid: str) -> str | None:
    data = load_session(sid)
    if data is None:
        return None
    return data.get("last_seen_ts")


def update_last_seen_ts(sid: str, ts: str) -> None:
    data = load_session(sid) or {}
    data["last_seen_ts"] = ts
    save_session(sid, data)

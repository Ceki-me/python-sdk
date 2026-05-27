from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceki_sdk import Browser
from ceki_sdk._state import save_session, load_session, get_last_seen_ts, update_last_seen_ts


def _make_browser():
    client = AsyncMock()
    client._active_browsers = {}
    client.chat_url = "https://test/chat"
    client.api_key = "test"

    match = AsyncMock()
    match.session_id = "persist-1"
    match.schedule_id = 1
    match.chat_topic_id = "t1"
    match.browser_info = {}
    match.provider_user_id = None

    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        b = Browser(client, match)
    return b


def _make_chat_msg(msg_id: str, text: str, ts: str):
    from ceki_sdk._models import ChatMessage
    return ChatMessage(
        _id=msg_id,
        topic_id="t1",
        sender_id=99,
        text=text,
        type="text",
        created_at=ts,
    )


async def test_snapshot_filters_old_messages_client_side(tmp_path: Path):
    """Two sequential snapshots: second one returns no old messages even if
    chat.history returns them (simulating server ignoring 'since' param)."""
    with patch("ceki_sdk._state._STATE_DIR", tmp_path / "sessions"):
        sid = "persist-1"
        save_session(sid, {"session_id": sid, "schedule_id": 1, "last_seen_ts": None})

        msg1 = _make_chat_msg("m1", "hello", "2026-01-01T00:00:01Z")
        msg2 = _make_chat_msg("m2", "world", "2026-01-01T00:00:02Z")
        msg3 = _make_chat_msg("m3", "new", "2026-01-01T00:00:03Z")

        png_data = base64.b64encode(b"\x89PNG").decode()

        # --- Process 1: first snapshot, gets 2 messages ---
        b1 = _make_browser()
        b1._last_seen_ts = get_last_seen_ts(sid)
        b1.send = AsyncMock(return_value={"data": png_data})
        b1.chat.history = AsyncMock(return_value=[msg1, msg2])

        snap1 = await b1.snapshot()
        assert len(snap1.chat) == 2
        assert b1._last_seen_ts == "2026-01-01T00:00:02Z"

        # Persist like CLI does
        if b1._last_seen_ts:
            update_last_seen_ts(sid, b1._last_seen_ts)

        # Verify state file
        assert get_last_seen_ts(sid) == "2026-01-01T00:00:02Z"

        # --- Process 2: second snapshot, server returns same messages (doesn't filter by since) ---
        b2 = _make_browser()
        b2._last_seen_ts = get_last_seen_ts(sid)
        b2.send = AsyncMock(return_value={"data": png_data})
        b2.chat.history = AsyncMock(return_value=[msg1, msg2])

        snap2 = await b2.snapshot()
        assert len(snap2.chat) == 0, "Should filter out already-seen messages"
        assert b2._last_seen_ts == "2026-01-01T00:00:02Z"

        # --- Process 3: third snapshot, server returns old + new messages ---
        b3 = _make_browser()
        b3._last_seen_ts = get_last_seen_ts(sid)
        b3.send = AsyncMock(return_value={"data": png_data})
        b3.chat.history = AsyncMock(return_value=[msg1, msg2, msg3])

        snap3 = await b3.snapshot()
        assert len(snap3.chat) == 1, "Should return only new message"
        assert snap3.chat[0].id == "m3"
        assert b3._last_seen_ts == "2026-01-01T00:00:03Z"

        if b3._last_seen_ts:
            update_last_seen_ts(sid, b3._last_seen_ts)
        assert get_last_seen_ts(sid) == "2026-01-01T00:00:03Z"


async def test_snapshot_no_last_seen_returns_all(tmp_path: Path):
    """First ever snapshot (no last_seen_ts) returns all messages."""
    with patch("ceki_sdk._state._STATE_DIR", tmp_path / "sessions"):
        msg1 = _make_chat_msg("m1", "hi", "2026-01-01T00:00:01Z")
        png_data = base64.b64encode(b"\x89PNG").decode()

        b = _make_browser()
        b._last_seen_ts = None
        b.send = AsyncMock(return_value={"data": png_data})
        b.chat.history = AsyncMock(return_value=[msg1])

        snap = await b.snapshot()
        assert len(snap.chat) == 1
        assert b._last_seen_ts == "2026-01-01T00:00:01Z"

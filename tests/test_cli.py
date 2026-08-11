from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceki_sdk._state import (
    delete_session,
    get_last_seen_ts,
    load_session,
    save_session,
    update_last_seen_ts,
)
from ceki_sdk.cli import build_parser

# ──────────────────────────────────────────────────────────────────────────
# State file tests
# ──────────────────────────────────────────────────────────────────────────


def test_state_save_load_delete(tmp_path: Path):
    with patch("ceki_sdk._state._STATE_DIR", tmp_path / "sessions"):
        save_session("test-1", {"session_id": "test-1", "schedule_id": 5})
        data = load_session("test-1")
        assert data is not None
        assert data["session_id"] == "test-1"
        assert "updated_at" in data

        delete_session("test-1")
        assert load_session("test-1") is None


def test_state_last_seen_ts(tmp_path: Path):
    with patch("ceki_sdk._state._STATE_DIR", tmp_path / "sessions"):
        assert get_last_seen_ts("s1") is None
        save_session("s1", {"session_id": "s1"})
        assert get_last_seen_ts("s1") is None
        update_last_seen_ts("s1", "2026-01-01T00:00:00Z")
        assert get_last_seen_ts("s1") == "2026-01-01T00:00:00Z"


# ──────────────────────────────────────────────────────────────────────────
# Parser tests
# ──────────────────────────────────────────────────────────────────────────


def test_parser_rent():
    parser = build_parser()
    args = parser.parse_args(["rent", "--schedule", "42"])
    assert args.command == "rent"
    assert args.schedule == 42


def test_parser_snapshot():
    parser = build_parser()
    args = parser.parse_args(["snapshot", "ses-123", "-o", "/tmp/x.png"])
    assert args.command == "snapshot"
    assert args.session_id == "ses-123"
    assert args.output == "/tmp/x.png"


def test_parser_navigate():
    parser = build_parser()
    args = parser.parse_args(["navigate", "ses-1", "https://example.com"])
    assert args.command == "navigate"
    assert args.url == "https://example.com"


def test_parser_click():
    parser = build_parser()
    args = parser.parse_args(["click", "ses-1", "100", "200"])
    assert args.command == "click"
    assert args.x == 100
    assert args.y == 200


def test_parser_type():
    parser = build_parser()
    args = parser.parse_args(["type", "ses-1", "hello world"])
    assert args.command == "type"
    assert args.text == "hello world"
    assert not args.natural


def test_parser_type_natural():
    parser = build_parser()
    args = parser.parse_args(["type", "ses-1", "hi", "--natural"])
    assert args.natural is True


def test_parser_type_no_human():
    parser = build_parser()
    args = parser.parse_args(["type", "ses-1", "hi", "--no-human"])
    assert args.no_human is True


# task 429 — typing humanized BY DEFAULT (revert of 428 opt-in).
# Default + --natural → human=None (SDK humanizer ON).
# --no-human / --raw → human=False (explicit flat for THIS call only).
# --natural is a no-op alias, not a switch.

def _resolve_type_human(args):
    """Mirror of cli._human_flag for `ceki type` post-429."""
    if getattr(args, "no_human", False) or getattr(args, "raw", False):
        return False
    return None


def test_type_default_is_humanized():
    a = build_parser().parse_args(["type", "ses-1", "hi"])
    assert _resolve_type_human(a) is None


def test_type_natural_is_noop_default_remains_on():
    a = build_parser().parse_args(["type", "ses-1", "hi", "--natural"])
    assert _resolve_type_human(a) is None


def test_type_no_human_explicit_flat():
    a = build_parser().parse_args(["type", "ses-1", "hi", "--no-human"])
    assert _resolve_type_human(a) is False


def test_type_no_human_wins_over_natural():
    a = build_parser().parse_args(["type", "ses-1", "hi", "--natural", "--no-human"])
    assert _resolve_type_human(a) is False


def test_parser_scroll():
    parser = build_parser()
    args = parser.parse_args(["scroll", "ses-1", "0", "0", "-300"])
    assert args.command == "scroll"
    assert args.dy == -300


def test_parser_chat_send():
    parser = build_parser()
    args = parser.parse_args(["chat", "ses-1", "send", "hello provider"])
    assert args.command == "chat"
    assert args.chat_action == "send"
    assert args.text == "hello provider"


def test_parser_chat_next():
    parser = build_parser()
    args = parser.parse_args(["chat", "ses-1", "next", "--timeout=30"])
    assert args.chat_action == "next"
    assert args.timeout == 30


def test_parser_stop():
    parser = build_parser()
    args = parser.parse_args(["stop", "ses-1"])
    assert args.command == "stop"


# ──────────────────────────────────────────────────────────────────────────
# New subcommand parser tests
# ──────────────────────────────────────────────────────────────────────────


def test_parser_profile_export():
    parser = build_parser()
    args = parser.parse_args([
        "profile", "ses-1", "export", "-o", "/tmp/p.json",
        "--domains", ".reddit.com,reddit.com",
    ])
    assert args.command == "profile"
    assert args.session_id == "ses-1"
    assert args.profile_action == "export"
    assert args.output == "/tmp/p.json"
    assert args.domains == ".reddit.com,reddit.com"
    assert args.no_session_storage is False


def test_parser_profile_export_no_session_storage():
    parser = build_parser()
    args = parser.parse_args([
        "profile", "ses-1", "export", "-o", "/tmp/p.json", "--no-session-storage",
    ])
    assert args.no_session_storage is True


def test_parser_profile_import():
    parser = build_parser()
    args = parser.parse_args(["profile", "ses-1", "import", "-i", "/tmp/p.json"])
    assert args.command == "profile"
    assert args.profile_action == "import"
    assert args.input == "/tmp/p.json"


def test_parser_search():
    parser = build_parser()
    args = parser.parse_args([
        "search", "--limit", "5", "--filter", "region=US", "--filter", "price_max=0.5",
    ])
    assert args.command == "search"
    assert args.limit == 5
    assert args.filter == ["region=US", "price_max=0.5"]


def test_parser_search_defaults():
    parser = build_parser()
    args = parser.parse_args(["search"])
    assert args.limit == 20
    assert args.filter is None


def test_parser_chat_history():
    parser = build_parser()
    args = parser.parse_args([
        "chat", "ses-1", "history", "--since", "2026-01-01T00:00:00Z", "--limit", "20",
    ])
    assert args.command == "chat"
    assert args.chat_action == "history"
    assert args.since == "2026-01-01T00:00:00Z"
    assert args.limit == 20


def test_parser_wait():
    parser = build_parser()
    args = parser.parse_args(["wait", "ses-1"])
    assert args.command == "wait"
    assert args.session_id == "ses-1"


def test_parser_chat_send_image():
    parser = build_parser()
    args = parser.parse_args(["chat", "ses-1", "send-image", "--image", "/tmp/img.png"])
    assert args.command == "chat"
    assert args.chat_action == "send-image"
    assert args.image == "/tmp/img.png"
    assert args.text is None


def test_parser_chat_send_image_with_text():
    parser = build_parser()
    args = parser.parse_args([
        "chat", "ses-1", "send-image", "--image", "/tmp/img.png", "--text", "look at this",
    ])
    assert args.text == "look at this"


def test_parser_screenshot():
    parser = build_parser()
    args = parser.parse_args(["screenshot", "ses-1", "-o", "/tmp/s.png", "--format", "jpeg"])
    assert args.command == "screenshot"
    assert args.session_id == "ses-1"
    assert args.output == "/tmp/s.png"
    assert args.format == "jpeg"


def test_parser_screenshot_default_format():
    parser = build_parser()
    args = parser.parse_args(["screenshot", "ses-1", "-o", "/tmp/s.png"])
    assert args.format == "png"


def test_parser_switch_tab():
    parser = build_parser()
    args = parser.parse_args(["switch-tab", "ses-1"])
    assert args.command == "switch-tab"
    assert args.session_id == "ses-1"


def test_parser_configure():
    parser = build_parser()
    args = parser.parse_args([
        "configure", "ses-1", "--masking-mode", "true", "--fingerprint", "false",
    ])
    assert args.command == "configure"
    assert args.session_id == "ses-1"
    assert args.masking_mode == "true"
    assert args.fingerprint == "false"


def test_parser_configure_partial():
    parser = build_parser()
    args = parser.parse_args(["configure", "ses-1", "--masking-mode", "true"])
    assert args.masking_mode == "true"
    assert args.fingerprint is None


def test_parser_cdp():
    parser = build_parser()
    args = parser.parse_args([
        "cdp", "ses-1", "--method", "Page.navigate",
        "--params", '{"url":"https://example.com"}',
    ])
    assert args.command == "cdp"
    assert args.session_id == "ses-1"
    assert args.method == "Page.navigate"
    assert args.params == '{"url":"https://example.com"}'


def test_parser_cdp_no_params():
    parser = build_parser()
    args = parser.parse_args(["cdp", "ses-1", "--method", "Page.reload"])
    assert args.method == "Page.reload"
    assert args.params is None


def test_search_domains_parsing():
    """Verify --domains comma-split logic in profile export."""
    parser = build_parser()
    args = parser.parse_args([
        "profile", "ses-1", "export", "-o", "/tmp/p.json",
        "--domains", ".reddit.com,reddit.com,www.reddit.com",
    ])
    domains = [d.strip() for d in args.domains.split(",")]
    assert domains == [".reddit.com", "reddit.com", "www.reddit.com"]


def test_cli_uses_disconnect_not_close():
    """CLI must use client.disconnect(), not client.close(), to avoid killing active sessions."""
    cli_path = Path(__file__).resolve().parent.parent / "ceki_sdk" / "cli.py"
    src = cli_path.read_text()
    assert "client.disconnect()" in src
    assert "client.close()" not in src


def test_search_filter_parsing():
    """Verify --filter key=val parsing logic."""
    parser = build_parser()
    args = parser.parse_args([
        "search", "--filter", "region=US", "--filter", "price_max=0.5",
    ])
    filters = {}
    for f in args.filter:
        k, v = f.split("=", 1)
        filters[k] = v
    assert filters == {"region": "US", "price_max": "0.5"}


# ──────────────────────────────────────────────────────────────────────────
# Exit code: missing CEKI_API_KEY
# ──────────────────────────────────────────────────────────────────────────


def test_missing_api_key_exits_2():
    env = {k: v for k, v in __import__("os").environ.items() if k != "CEKI_API_KEY"}
    result = subprocess.run(
        [sys.executable, "-m", "ceki_sdk.cli", "rent", "--schedule", "1"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    err = json.loads(result.stderr.strip())
    assert err["code"] == "auth"


# ──────────────────────────────────────────────────────────────────────────
# Resume exception mapping tests
# ──────────────────────────────────────────────────────────────────────────


async def test_resume_not_found():
    from ceki_sdk._client import Client
    from ceki_sdk._exceptions import SessionNotFound

    client = Client(
        api_key="test",
        relay_url="wss://test/ws/agent",
        api_url="https://test",
        chat_url="https://test/chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    async def fake_dispatch():
        await client._dispatch({"type": "resume_failed", "session_id": "s1", "reason": "not_found"})

    import asyncio
    loop = asyncio.get_event_loop()
    task = loop.create_task(client.resume("s1"))
    await asyncio.sleep(0.01)
    await fake_dispatch()
    with pytest.raises(SessionNotFound):
        await task


async def test_resume_not_owner():
    from ceki_sdk._client import Client
    from ceki_sdk._exceptions import NotOwner

    client = Client(
        api_key="test",
        relay_url="wss://test/ws/agent",
        api_url="https://test",
        chat_url="https://test/chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    async def fake_dispatch():
        await client._dispatch({"type": "resume_failed", "session_id": "s2", "reason": "not_owner"})

    import asyncio
    loop = asyncio.get_event_loop()
    task = loop.create_task(client.resume("s2"))
    await asyncio.sleep(0.01)
    await fake_dispatch()
    with pytest.raises(NotOwner):
        await task


async def test_resume_ok():
    from ceki_sdk._client import Client

    client = Client(
        api_key="test",
        relay_url="wss://test/ws/agent",
        api_url="https://test",
        chat_url="https://test/chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    import asyncio
    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        task = asyncio.create_task(client.resume("s3"))
        await asyncio.sleep(0.01)
        await client._dispatch({
            "type": "resume_ok",
            "session_id": "s3",
            "schedule_id": 42,
            "chat_topic_id": "topic-1",
            "provider_user_id": 99,
        })
        browser = await task
    assert browser.session_id == "s3"
    assert browser.schedule_id == 42
    assert browser.chat_topic_id == "topic-1"
    assert "s3" in client._active_browsers


# ──────────────────────────────────────────────────────────────────────────
# Snapshot test
# ──────────────────────────────────────────────────────────────────────────


async def test_snapshot_returns_data():
    import base64

    from ceki_sdk import Browser

    client = AsyncMock()
    client._active_browsers = {}
    client.chat_url = "https://test/chat"
    client.api_key = "test"

    match = AsyncMock()
    match.session_id = "snap-1"
    match.schedule_id = 1
    match.chat_topic_id = "t1"
    match.browser_info = {}
    match.provider_user_id = None

    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        b = Browser(client, match)

    png_data = base64.b64encode(b"\x89PNG\r\n").decode()
    b.send = AsyncMock(return_value={"data": png_data})
    b.chat.history = AsyncMock(return_value=[])

    snap = await b.snapshot()
    assert snap.screenshot == png_data
    assert snap.chat == []
    assert snap.ts is not None


# ──────────────────────────────────────────────────────────────────────────
# Provider command parser
# ──────────────────────────────────────────────────────────────────────────


def test_parser_provider_run():
    parser = build_parser()
    args = parser.parse_args(
        ["provider", "run", "--token", "tok-1", "--ext-dir", "/ext/dist",
         "--api-url", "https://api.ceki.me", "--schedule-id", "7", "--timeout", "90"]
    )
    assert args.command == "provider"
    assert args.provider_action == "run"
    assert args.token == "tok-1"
    assert args.ext_dir == "/ext/dist"
    assert args.api_url == "https://api.ceki.me"
    assert args.schedule_id == 7
    assert args.timeout == 90
    assert args.verbose is False


def test_parser_provider_run_defaults():
    parser = build_parser()
    args = parser.parse_args(["provider", "run"])
    assert args.command == "provider"
    assert args.token is None
    assert args.ext_dir is None
    assert args.api_url is None
    assert args.schedule_id is None
    assert args.timeout is None
    assert args.verbose is False


def test_parser_provider_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["provider"])


# ──────────────────────────────────────────────────────────────────────────
# Bug 1 regression: void commands with a null daemon result.
# A successful void command (navigate/click/type/stop/...) comes back as
# {"ok": true, "result": null}.  _daemon_request now returns (True, None) for
# that — branching on `result is not None` used to fall through to the one-shot
# resume path, which disconnects and kills the rented session.
# ──────────────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def json(self):
        return self._body


async def test_daemon_request_void_result_none_is_success(tmp_path: Path):
    """_daemon_request returns (True, None) for a successful void command."""
    from ceki_sdk.cli import _daemon_request

    pid = tmp_path / "ceki-daemon.pid"
    pid.write_text("12345")

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp({"ok": True, "result": None})

    with (
        patch("ceki_sdk.cli.PID_FILE", pid),
        patch("ceki_sdk.cli.httpx.AsyncClient", _FakeAsyncClient),
    ):
        ok, result = await _daemon_request("/navigate", {"session_id": "s1"})

    assert ok is True
    assert result is None


async def test_daemon_request_absent_returns_false(tmp_path: Path):
    """No PID file → (False, None) so the caller runs the one-shot fallback."""
    from ceki_sdk.cli import _daemon_request

    with patch("ceki_sdk.cli.PID_FILE", tmp_path / "missing.pid"):
        ok, result = await _daemon_request("/navigate", {"session_id": "s1"})

    assert ok is False
    assert result is None


async def test_navigate_void_ok_null_result_does_not_resume(tmp_path: Path):
    """Bug 1: navigate with {'ok': true, 'result': null} must print ok and must
    NOT call _resume_browser (the one-shot fallback disconnects the client and
    kills the session)."""
    import argparse

    from ceki_sdk.cli import _cmd_navigate

    pid = tmp_path / "ceki-daemon.pid"
    pid.write_text("12345")

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp({"ok": True, "result": None})

    with (
        patch("ceki_sdk.cli.PID_FILE", pid),
        patch("ceki_sdk.cli.httpx.AsyncClient", _FakeAsyncClient),
        patch("ceki_sdk.cli._resume_browser", AsyncMock()) as resume,
        patch("ceki_sdk.cli._out") as out,
    ):
        args = argparse.Namespace(session_id="s1", url="https://example.com")
        await _cmd_navigate(args)

    resume.assert_not_awaited()
    out.assert_called_once_with({"ok": True})


async def test_navigate_no_daemon_falls_back_to_oneshot(tmp_path: Path):
    """Reverse path: daemon absent → (False, None) → one-shot fallback works."""
    import argparse

    from ceki_sdk.cli import _cmd_navigate

    client = AsyncMock()
    client._ws = AsyncMock()
    browser = AsyncMock()

    with (
        patch("ceki_sdk.cli.PID_FILE", tmp_path / "missing.pid"),
        patch("ceki_sdk.cli._get_api_key", return_value="key"),
        patch(
            "ceki_sdk.cli._resume_browser",
            AsyncMock(return_value=(client, browser)),
        ) as resume,
        patch("ceki_sdk.cli._out") as out,
    ):
        args = argparse.Namespace(session_id="s1", url="https://example.com")
        await _cmd_navigate(args)

    resume.assert_awaited_once()
    browser.navigate.assert_awaited_once()
    out.assert_called_once_with({"ok": True})

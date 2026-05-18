from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import connect, ConnectOptions
from ._exceptions import (
    AuthFailed,
    CaptchaTimeoutError,
    CekiError,
    ConnectionLost,
    SessionNotFound,
    SessionExpired,
    NotOwner,
)
from ._state import save_session, load_session, delete_session, get_last_seen_ts, update_last_seen_ts


def _out(data: Any) -> None:
    json.dump(data, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _err(error: str, code: str = "error") -> None:
    json.dump({"error": error, "code": code}, sys.stderr)
    sys.stderr.write("\n")
    sys.stderr.flush()


def _get_api_key() -> str:
    key = os.environ.get("CEKI_API_KEY")
    if not key:
        _err("CEKI_API_KEY not set", "auth")
        sys.exit(2)
    return key


def _connect_options() -> ConnectOptions:
    opts = ConnectOptions(reconnect=False)
    if os.environ.get("CEKI_API_URL"):
        opts.api_url = os.environ["CEKI_API_URL"]
    if os.environ.get("CEKI_RELAY_URL"):
        opts.relay_url = os.environ["CEKI_RELAY_URL"]
    if os.environ.get("CEKI_CHAT_URL"):
        opts.chat_url = os.environ["CEKI_CHAT_URL"]
    ba_user = os.environ.get("CEKI_BASIC_AUTH_USER")
    ba_pass = os.environ.get("CEKI_BASIC_AUTH_PASS")
    if ba_user and ba_pass:
        opts.basic_auth = (ba_user, ba_pass)
    return opts


async def _cmd_rent(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    fp_data: bool | dict = True
    if args.fingerprint_from:
        with open(args.fingerprint_from) as f:
            profile = json.load(f)
        fp_data = profile.get("fingerprint") or True
    client = await connect(api_key, _connect_options())
    try:
        browser = await client.rent(args.schedule, mode=args.mode, fingerprint=fp_data)
        save_session(browser.session_id, {
            "session_id": browser.session_id,
            "chat_topic_id": browser.chat_topic_id,
            "schedule_id": browser.schedule_id,
            "last_seen_ts": None,
        })
        _out({
            "session_id": browser.session_id,
            "chat_topic_id": browser.chat_topic_id,
            "schedule_id": browser.schedule_id,
        })
    finally:
        if client._ws:
            await client.disconnect()


async def _resume_browser(api_key: str, session_id: str):
    client = await connect(api_key, _connect_options())
    browser = await client.resume(session_id)
    return client, browser


async def _cmd_snapshot(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        last_seen = get_last_seen_ts(args.session_id)
        browser._last_seen_ts = last_seen
        snap = await browser.snapshot()
        import base64
        png_bytes = base64.b64decode(snap.screenshot) if snap.screenshot else b""
        out_path = args.output
        with open(out_path, "wb") as f:
            f.write(png_bytes)
        if browser._last_seen_ts:
            update_last_seen_ts(args.session_id, browser._last_seen_ts)
        chat_list = [{"from": m.sender_id, "text": m.text, "ts": m.created_at} for m in snap.chat]
        _out({"screenshot": out_path, "chat": chat_list, "ts": snap.ts.isoformat()})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_navigate(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.navigate(args.url)
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_click(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.click(args.x, args.y)
        _out({"ok": True, "pointer": [args.x, args.y]})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_type(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    human = "natural" if args.natural else None
    client, browser = await _resume_browser(api_key, args.session_id)
    if human is None:
        browser.set_human(None)
    try:
        await browser.type(args.text)
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_scroll(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.scroll(args.x, args.y, delta_y=args.dy)
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_chat(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        if args.chat_action == "send":
            result = await browser.chat.send(args.text)
            _out({"ok": True, "message_id": result.get("message_id")})
        elif args.chat_action == "next":
            last_seen = get_last_seen_ts(args.session_id)
            msgs = await browser.chat.history(since=last_seen)
            if msgs:
                m = msgs[0]
                update_last_seen_ts(args.session_id, m.created_at)
                _out({"from": m.sender_id, "text": m.text, "ts": m.created_at})
            else:
                got_msg = asyncio.Event()
                result_msg: dict = {}

                async def on_msg(msg):
                    nonlocal result_msg
                    result_msg = {"from": msg.sender_id, "text": msg.text, "ts": msg.created_at}
                    got_msg.set()

                browser.chat.on_message(on_msg)
                try:
                    await asyncio.wait_for(got_msg.wait(), timeout=args.timeout)
                    update_last_seen_ts(args.session_id, result_msg["ts"])
                    _out(result_msg)
                except asyncio.TimeoutError:
                    _out(None)
        elif args.chat_action == "history":
            since = None
            if args.since:
                try:
                    ts_val = float(args.since)
                    from datetime import datetime, timezone
                    since = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
                except ValueError:
                    since = args.since
            msgs = await browser.chat.history(since=since, limit=args.limit)
            _out([{"from": m.sender_id, "text": m.text, "ts": m.created_at} for m in msgs])
        elif args.chat_action == "send-image":
            if args.text:
                await browser.chat.send(args.text)
            result = await browser.chat.send_image(Path(args.image))
            _out({"ok": True, "message_id": result.get("message_id")})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_stop(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.close()
        delete_session(args.session_id)
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_profile(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        if args.profile_action == "export":
            domains = None
            if args.domains:
                domains = [d.strip() for d in args.domains.split(",")]
            include_session_storage = not args.no_session_storage
            profile = await browser.profile.export(
                domains=domains,
                include_session_storage=include_session_storage,
            )
            with open(args.output, "w") as f:
                json.dump(profile, f)
            _out({"ok": True, "path": args.output})
        elif args.profile_action == "import":
            with open(args.input, "r") as f:
                profile_dict = json.load(f)
            await browser.profile.import_(profile_dict)
            _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_sessions(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client = await connect(api_key, _connect_options())
    try:
        active = not getattr(args, "all", False)
        limit = getattr(args, "limit", 50)
        results = await client.list_sessions(active=active, limit=limit)
        if getattr(args, "json", False):
            _out([r.model_dump() for r in results])
        else:
            if not results:
                print("No sessions found.")
                return
            header = f"{'SID':<8}{'SCHEDULE':<10}{'STARTED':<22}{'DURATION':<10}{'EARNED':<9}{'STATUS':<10}{'RENTER':<16}{'PROVIDER'}"
            print(header)
            for s in results:
                started = (s.started_at.strftime("%Y-%m-%dT%H:%M:%SZ") if s.started_at else "—")
                mins, secs = divmod(s.duration, 60)
                dur = f"{mins}:{secs:02d}"
                earned = f"${s.earned:.2f}"
                renter = s.renter.get("name", "—") if s.renter else "—"
                provider = s.provider.get("name", "—") if s.provider else "—"
                print(f"{s.id:<8}{s.schedule_id:<10}{started:<22}{dur:<10}{earned:<9}{s.status:<10}{renter:<16}{provider}")
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_my_browsers(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client = await connect(api_key, _connect_options())
    try:
        results = await client.my_browsers()
        _out([r.model_dump() for r in results])
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_search(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client = await connect(api_key, _connect_options())
    try:
        filters: dict[str, str] = {}
        for f in (args.filter or []):
            k, v = f.split("=", 1)
            filters[k] = v
        results = await client.search(filters=filters, limit=args.limit)
        _out([r.model_dump() for r in results])
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_wait(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        reason = await browser.wait_until_ended()
        _out({"ended": True, "reason": reason})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_screenshot(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        data = await browser.screenshot(format="png", full_page=args.full)
        with open(args.output, "wb") as f:
            f.write(data)
        _out({"ok": True, "path": args.output})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_switch_tab(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.switch_tab()
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_configure(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        kwargs: dict[str, Any] = {}
        if args.masking_mode is not None:
            kwargs["masking_mode"] = args.masking_mode
        if args.fingerprint is not None:
            kwargs["fingerprint"] = args.fingerprint
        await browser.configure(**kwargs)
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_upload(args: argparse.Namespace) -> None:
    file_path = Path(args.file_path)
    if not file_path.is_file():
        _err(f"file not found: {args.file_path}")
        sys.exit(1)
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        result = await browser.upload(
            args.selector, file_path, filename=args.filename
        )
        _out(result)
    except ValueError as e:
        _err(str(e))
        sys.exit(1)
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_request_captcha(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        auto = not args.manual
        result = await browser.request_captcha(
            acceptance_timeout=args.acceptance,
            completion_timeout=args.completion,
            auto_accept=auto,
        )
        _out(result.to_dict())
        if not result.solved:
            sys.exit(1)
    except CaptchaTimeoutError as e:
        _out({"solved": False, "cancel_reason": f"timeout:{e.phase}", "child_event_id": None, "correction_id": None})
        sys.exit(1)
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_cdp(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        params = json.loads(args.params) if args.params else {}
        result = await browser.send({"method": args.method, "params": params})
        _out(result)
    finally:
        if client._ws:
            await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ceki-browser", description="CLI for ceki.me browser rental")

    sub = parser.add_subparsers(dest="command", required=True)

    p_rent = sub.add_parser("rent", help="Rent a browser")
    p_rent.add_argument("--schedule", type=int, required=True, help="Schedule ID")
    p_rent.add_argument("--mode", choices=["incognito", "main"], default="incognito", help="Profile mode (default: incognito)")
    p_rent.add_argument("--fingerprint-from", help="Path to profile JSON with fingerprint data")

    p_snap = sub.add_parser("snapshot", help="Take screenshot + get new chat messages")
    p_snap.add_argument("session_id", help="Session ID")
    p_snap.add_argument("-o", "--output", required=True, help="Output PNG path")

    p_nav = sub.add_parser("navigate", help="Navigate to URL")
    p_nav.add_argument("session_id", help="Session ID")
    p_nav.add_argument("url", help="URL to navigate to")

    p_click = sub.add_parser("click", help="Click at coordinates")
    p_click.add_argument("session_id", help="Session ID")
    p_click.add_argument("x", type=int, help="X coordinate")
    p_click.add_argument("y", type=int, help="Y coordinate")

    p_type = sub.add_parser("type", help="Type text")
    p_type.add_argument("session_id", help="Session ID")
    p_type.add_argument("text", help="Text to type")
    p_type.add_argument("--natural", action="store_true", help="Enable human-like typing")

    p_scroll = sub.add_parser("scroll", help="Scroll")
    p_scroll.add_argument("session_id", help="Session ID")
    p_scroll.add_argument("x", type=int, help="X origin")
    p_scroll.add_argument("y", type=int, help="Y origin")
    p_scroll.add_argument("dy", type=int, help="Delta Y (negative = scroll down)")

    p_chat = sub.add_parser("chat", help="Chat with provider")
    p_chat.add_argument("session_id", help="Session ID")
    chat_sub = p_chat.add_subparsers(dest="chat_action", required=True)

    p_send = chat_sub.add_parser("send", help="Send message")
    p_send.add_argument("text", help="Message text")

    p_next = chat_sub.add_parser("next", help="Wait for next message")
    p_next.add_argument("--timeout", type=float, default=60, help="Timeout in seconds")

    p_history = chat_sub.add_parser("history", help="Get chat history")
    p_history.add_argument("--since", help="Timestamp (Unix or ISO-8601)")
    p_history.add_argument("--limit", type=int, default=50, help="Max messages")

    p_send_image = chat_sub.add_parser("send-image", help="Send image to chat")
    p_send_image.add_argument("--image", required=True, help="Path to image file")
    p_send_image.add_argument("--text", help="Optional text to send before image")

    p_stop = sub.add_parser("stop", help="End session")
    p_stop.add_argument("session_id", help="Session ID")

    p_profile = sub.add_parser("profile", help="Profile export/import")
    p_profile.add_argument("session_id", help="Session ID")
    profile_sub = p_profile.add_subparsers(dest="profile_action", required=True)

    p_profile_export = profile_sub.add_parser("export", help="Export profile to file")
    p_profile_export.add_argument("-o", "--output", required=True, help="Output JSON path")
    p_profile_export.add_argument("--domains", help="Comma-separated domain filter")
    p_profile_export.add_argument(
        "--no-session-storage", action="store_true", help="Exclude sessionStorage"
    )

    p_profile_import = profile_sub.add_parser("import", help="Import profile from file")
    p_profile_import.add_argument("-i", "--input", required=True, help="Input JSON path")

    p_sessions = sub.add_parser("sessions", help="List agent sessions (active by default)")
    p_sessions.add_argument("--all", action="store_true", help="Show all sessions, not just active")
    p_sessions.add_argument("--limit", type=int, default=50, help="Max results")
    p_sessions.add_argument("--json", action="store_true", help="Raw JSON output")

    sub.add_parser("my-browsers", help="List browsers with pre-arranged rent contracts")

    p_search = sub.add_parser("search", help="Search available browsers")
    p_search.add_argument("--limit", type=int, default=20, help="Max results")
    p_search.add_argument("--filter", action="append", help="Filter key=val (repeatable)")

    p_wait = sub.add_parser("wait", help="Wait until session ends")
    p_wait.add_argument("session_id", help="Session ID")

    p_screenshot = sub.add_parser("screenshot", help="Take screenshot and save to file")
    p_screenshot.add_argument("session_id", help="Session ID")
    p_screenshot.add_argument("-o", "--output", required=True, help="Output file path")
    p_screenshot.add_argument(
        "--format", choices=["png", "jpeg"], default="png", help="Image format"
    )
    p_screenshot.add_argument(
        "--full", action="store_true", default=False, help="Capture full page, not just viewport"
    )

    p_switch_tab = sub.add_parser("switch-tab", help="Switch browser tab")
    p_switch_tab.add_argument("session_id", help="Session ID")

    p_configure = sub.add_parser("configure", help="Configure session settings")
    p_configure.add_argument("session_id", help="Session ID")
    p_configure.add_argument("--masking-mode", help="Masking mode (true/false)")
    p_configure.add_argument("--fingerprint", help="Fingerprint (true/false)")

    p_upload = sub.add_parser("upload", help="Upload file to input[type=file]")
    p_upload.add_argument("session_id")
    p_upload.add_argument("--selector", required=True, help="CSS selector for file input")
    p_upload.add_argument("--file", required=True, dest="file_path", help="Path to file")
    p_upload.add_argument("--filename", help="Override filename (default: basename)")

    p_captcha = sub.add_parser("request-captcha", help="Request human to solve captcha")
    p_captcha.add_argument("session_id", help="Session ID")
    p_captcha.add_argument("--acceptance", type=float, default=60, help="Acceptance timeout sec (min 30)")
    p_captcha.add_argument("--completion", type=float, default=120, help="Completion timeout sec (min 30)")
    p_captcha.add_argument("--manual", action="store_true", help="Disable auto-accept (agent votes manually)")

    p_cdp = sub.add_parser("cdp", help="Send raw CDP command")
    p_cdp.add_argument("session_id", help="Session ID")
    p_cdp.add_argument("--method", required=True, help="CDP method name")
    p_cdp.add_argument("--params", help="CDP params as JSON string")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    handlers = {
        "rent": _cmd_rent,
        "snapshot": _cmd_snapshot,
        "navigate": _cmd_navigate,
        "click": _cmd_click,
        "type": _cmd_type,
        "scroll": _cmd_scroll,
        "chat": _cmd_chat,
        "stop": _cmd_stop,
        "profile": _cmd_profile,
        "sessions": _cmd_sessions,
        "my-browsers": _cmd_my_browsers,
        "search": _cmd_search,
        "wait": _cmd_wait,
        "screenshot": _cmd_screenshot,
        "switch-tab": _cmd_switch_tab,
        "configure": _cmd_configure,
        "cdp": _cmd_cdp,
        "upload": _cmd_upload,
        "request-captcha": _cmd_request_captcha,
    }

    handler = handlers.get(args.command)
    if not handler:
        _err(f"Unknown command: {args.command}")
        sys.exit(1)

    try:
        asyncio.run(handler(args))
    except (SessionNotFound, SessionExpired) as e:
        _err(str(e), "session_not_found")
        sys.exit(3)
    except NotOwner as e:
        _err(str(e), "not_owner")
        sys.exit(3)
    except TimeoutError as e:
        _err(str(e), "timeout")
        sys.exit(4)
    except (ConnectionLost, AuthFailed, ConnectionError, OSError) as e:
        _err(str(e), "network")
        sys.exit(5)
    except CekiError as e:
        _err(str(e), "ceki_error")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        _err(str(e), "error")
        sys.exit(1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from . import connect, ConnectOptions
from ._exceptions import (
    AuthFailed,
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
    client = await connect(api_key, _connect_options())
    try:
        browser = await client.rent(args.schedule)
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
            await client._ws.close()


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
            await client._ws.close()


async def _cmd_navigate(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.navigate(args.url)
        _out({"ok": True})
    finally:
        if client._ws:
            await client._ws.close()


async def _cmd_click(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.click(args.x, args.y)
        _out({"ok": True, "pointer": [args.x, args.y]})
    finally:
        if client._ws:
            await client._ws.close()


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
            await client._ws.close()


async def _cmd_scroll(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.scroll(args.x, args.y, delta_y=args.dy)
        _out({"ok": True})
    finally:
        if client._ws:
            await client._ws.close()


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
    finally:
        if client._ws:
            await client._ws.close()


async def _cmd_stop(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.close()
        delete_session(args.session_id)
        _out({"ok": True})
    finally:
        if client._ws:
            await client._ws.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ceki-browser", description="CLI for ceki.me browser rental")

    sub = parser.add_subparsers(dest="command", required=True)

    p_rent = sub.add_parser("rent", help="Rent a browser")
    p_rent.add_argument("--schedule", type=int, required=True, help="Schedule ID")

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

    p_stop = sub.add_parser("stop", help="End session")
    p_stop.add_argument("session_id", help="Session ID")

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

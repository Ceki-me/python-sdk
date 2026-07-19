from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import ConnectOptions, connect
from ._exceptions import (
    AuthFailed,
    CaptchaTimeoutError,
    CekiError,
    ConnectionLost,
    NotOwner,
    SessionExpired,
    SessionNotFound,
)
from ._state import (
    delete_session,
    get_last_seen_ts,
    save_session,
    update_last_seen_ts,
)
from .daemon import DAEMON_HOST, PID_FILE, daemon_port, is_running


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


# ── Daemon IPC ──────────────────────────────────────────────────────────────


async def _daemon_request(
    path: str,
    params: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> Any:
    """Send an IPC request to a running daemon.

    Returns ``None`` when the daemon is not running (clean fallback for the
    caller).  Raises ``CekiError`` when the daemon *was* expected to be
    reachable but isn't — the caller shows the error to the user instead of
    falling back to one-shot mode.

    The function checks ``PID_FILE`` first as a fast-path; if absent there is
    no running daemon.  If present but unreachable we clean the stale file.
    """
    if not PID_FILE.exists():
        return None  # daemon not running → clean fallback

    port = daemon_port()
    url = f"http://{DAEMON_HOST}:{port}{path}"
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                url,
                content=json.dumps(params or {}).encode(),
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            body = resp.json()
            if not body.get("ok"):
                raise CekiError(body.get("error", "daemon error"))
            return body.get("result")
    except httpx.ConnectError:
        PID_FILE.unlink(missing_ok=True)
        return None  # stale PID → clean fallback
    except httpx.TimeoutException:
        raise CekiError("daemon not responding (timeout), start daemon first")
    except httpx.HTTPError as e:
        raise CekiError(f"daemon error: {e}")


# ── Daemon subcommands ───────────────────────────────────────────────────────


def _cmd_daemon_start() -> int:
    """Start the daemon as a detached subprocess."""
    if is_running():
        print("daemon already running (pid {})".format(PID_FILE.read_text().strip()))
        return 0

    log_path = Path("/tmp/ceki-daemon.log")
    log_file = log_path.open("a")

    proc = subprocess.Popen(
        [sys.executable, "-m", "ceki_sdk.daemon"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    # Give it a moment to start
    for _ in range(20):
        time.sleep(0.25)
        if is_running():
            print(f"daemon started (pid {proc.pid})")
            return 0
    # Process may still be alive — check one last time
    if is_running():
        print(f"daemon started (pid {proc.pid})")
        return 0
    print("daemon failed to start (check /tmp/ceki-daemon.log)", file=sys.stderr)
    return 1


def _cmd_daemon_stop() -> int:
    """Stop the daemon by sending SIGTERM."""
    if not PID_FILE.exists():
        print("daemon is not running", file=sys.stderr)
        return 0
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.25)
            if not is_running():
                print("daemon stopped")
                return 0
        # Force kill after 5s
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)
        print("daemon killed (SIGKILL)")
    except (ValueError, OSError) as e:
        PID_FILE.unlink(missing_ok=True)
        print(f"daemon stop: {e}", file=sys.stderr)
    return 0


def _cmd_daemon_status() -> int:
    """Show daemon status."""
    if is_running():
        pid = PID_FILE.read_text().strip()
        port = daemon_port()
        print(f"daemon running (pid {pid}, {DAEMON_HOST}:{port})")
    else:
        print("daemon is not running")
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    action = args.daemon_action
    if action == "start":
        return _cmd_daemon_start()
    if action == "stop":
        return _cmd_daemon_stop()
    if action == "status":
        return _cmd_daemon_status()
    print(f"unknown daemon action: {action}", file=sys.stderr)
    return 1


async def _cmd_rent(args: argparse.Namespace) -> None:
    # Try daemon IPC
    fp_from = str(Path(args.fingerprint_from).resolve()) if args.fingerprint_from else None
    try:
        result = await _daemon_request("/rent", {
            "schedule": args.schedule,
            "mode": args.mode,
            "fingerprint_from": fp_from,
        })
        if result is not None:
            sid = result["session_id"]
            save_session(sid, {
                "session_id": sid,
                "chat_topic_id": result.get("chat_topic_id"),
                "schedule_id": result.get("schedule_id"),
                "last_seen_ts": None,
            })
            _out(result)
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
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
    # Try daemon IPC
    try:
        result = await _daemon_request("/snapshot", {"session_id": args.session_id})
        if result is not None:
            png_bytes = base64.b64decode(result["screenshot"]) if result.get("screenshot") else b""
            out_path = args.output
            with open(out_path, "wb") as f:
                f.write(png_bytes)
            if result.get("ts"):
                update_last_seen_ts(args.session_id, result["ts"])
            _out({"screenshot": out_path, "chat": result.get("chat", []), "ts": result.get("ts")})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        last_seen = get_last_seen_ts(args.session_id)
        browser._last_seen_ts = last_seen
        snap = await browser.snapshot()
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


def _human_flag(args: argparse.Namespace) -> bool | None:
    # task 427 — humanization is default ON. --no-human / --raw on the
    # per-command parser (or root) requests raw mode for this single call.
    if getattr(args, "no_human", False) or getattr(args, "raw", False):
        return False
    return None


async def _cmd_navigate(args: argparse.Namespace) -> None:
    # Try daemon IPC
    try:
        result = await _daemon_request("/navigate", {
            "session_id": args.session_id,
            "url": args.url,
            "human": _human_flag(args),
        })
        if result is not None:
            _out({"ok": True})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.navigate(args.url, human=_human_flag(args))
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_click(args: argparse.Namespace) -> None:
    # Try daemon IPC
    try:
        result = await _daemon_request("/click", {
            "session_id": args.session_id,
            "x": args.x,
            "y": args.y,
            "human": _human_flag(args),
        })
        if result is not None:
            _out({"ok": True, "pointer": [args.x, args.y]})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.click(args.x, args.y, human=_human_flag(args))
        _out({"ok": True, "pointer": [args.x, args.y]})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_type(args: argparse.Namespace) -> None:
    # task 429 — typing is humanized BY DEFAULT in both modes (revert of
    # task 428 opt-in). --no-human / --raw → explicit flat for THIS call
    # only (the real BUG-B fix: stop the leak, but keep default-ON).
    # --natural is a no-op alias kept for backwards compatibility.
    # Try daemon IPC
    try:
        result = await _daemon_request("/type", {
            "session_id": args.session_id,
            "text": args.text,
            "selector": args.selector,
            "human": _human_flag(args),
        })
        if result is not None:
            _out({"ok": True})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.type(args.text, selector=args.selector, human=_human_flag(args))
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_scroll(args: argparse.Namespace) -> None:
    # Try daemon IPC
    try:
        result = await _daemon_request("/scroll", {
            "session_id": args.session_id,
            "x": args.x,
            "y": args.y,
            "dy": args.dy,
            "human": _human_flag(args),
        })
        if result is not None:
            _out({"ok": True})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.scroll(args.x, args.y, delta_y=args.dy, human=_human_flag(args))
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_chat(args: argparse.Namespace) -> None:
    # Try daemon IPC
    try:
        if args.chat_action == "send":
            result = await _daemon_request("/chat/send", {
                "session_id": args.session_id,
                "text": args.text,
            })
            if result is not None:
                _out({"ok": True, "message_id": result.get("message_id")})
                return
        elif args.chat_action == "next":
            last_seen = get_last_seen_ts(args.session_id)
            result = await _daemon_request("/chat/next", {
                "session_id": args.session_id,
                "timeout": args.timeout,
                "since": last_seen,
            })
            if result is not None:
                if result:  # has message
                    update_last_seen_ts(args.session_id, result["ts"])
                _out(result)  # None → no message
                return
        elif args.chat_action == "history":
            since = None
            if args.since:
                try:
                    ts_val = float(args.since)
                    from datetime import datetime, timezone
                    since = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
                except ValueError:
                    since = args.since
            result = await _daemon_request("/chat/history", {
                "session_id": args.session_id,
                "since": since,
                "limit": args.limit,
            })
            if result is not None:
                _out(result)
                return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot (all chat actions, including send-image)
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
    # Try daemon IPC
    try:
        result = await _daemon_request("/stop", {"session_id": args.session_id})
        if result is not None:
            delete_session(args.session_id)
            _out({"ok": True})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
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
    # Try daemon IPC
    try:
        if args.profile_action == "export":
            domains = ",".join(args.domains) if args.domains else None
            result = await _daemon_request("/profile/export", {
                "session_id": args.session_id,
                "domains": domains,
                "no_session_storage": args.no_session_storage,
            })
            if result is not None:
                with open(args.output, "w") as f:
                    json.dump(result, f)
                _out({"ok": True, "path": args.output})
                return
        elif args.profile_action == "import":
            with open(args.input, "r") as f:
                profile_dict = json.load(f)
            result = await _daemon_request("/profile/import", {
                "session_id": args.session_id,
                "profile": profile_dict,
            })
            if result is not None:
                _out({"ok": True})
                return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
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
            _out([r.model_dump(mode="json") for r in results])
        else:
            if not results:
                print("No sessions found.")
                return
            header = (
                f"{'SID':<8}{'SCHEDULE':<10}{'STARTED':<22}"
                f"{'DURATION':<10}{'EARNED':<9}{'STATUS':<10}"
                f"{'RENTER':<16}{'PROVIDER'}"
            )
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
        _out([r.model_dump(mode="json") for r in results])
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
        _out([r.model_dump(mode="json") for r in results])
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
    # Try daemon IPC
    try:
        result = await _daemon_request("/screenshot", {
            "session_id": args.session_id,
            "full": args.full,
        })
        if result is not None:
            data = base64.b64decode(result.get("data", ""))
            with open(args.output, "wb") as f:
                f.write(data)
            _out({"ok": True, "path": args.output})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        raw = await browser.screenshot(format="png", full_page=args.full)
        with open(args.output, "wb") as f:
            f.write(raw)
        _out({"ok": True, "path": args.output})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_switch_tab(args: argparse.Namespace) -> None:
    # Try daemon IPC
    try:
        result = await _daemon_request("/switch-tab", {"session_id": args.session_id})
        if result is not None:
            _out({"ok": True})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        await browser.switch_tab()
        _out({"ok": True})
    finally:
        if client._ws:
            await client.disconnect()


async def _cmd_configure(args: argparse.Namespace) -> None:
    # Try daemon IPC
    try:
        params: dict[str, Any] = {"session_id": args.session_id}
        if args.masking_mode is not None:
            params["masking_mode"] = args.masking_mode
        if args.fingerprint is not None:
            params["fingerprint"] = args.fingerprint
        result = await _daemon_request("/configure", params)
        if result is not None:
            _out({"ok": True})
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
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
            args.selector, file_path, filename=args.filename,
            mime_type=getattr(args, "mime_type", None),
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
        _out({
            "solved": False,
            "cancel_reason": f"timeout:{e.phase}",
            "child_event_id": None,
            "correction_id": None,
        })
        sys.exit(1)
    finally:
        if client._ws:
            await client.disconnect()


def _contract_client():
    from .contract import ContractClient
    return ContractClient()


def _parse_participant(spec: str) -> dict[str, Any]:
    """Parse 'agent:5:reviewer' / 'user:7:qa' / 'agent:5:role:42'.

    Returns {participable_id: int, participable_type: 'agent'|'user', role_id: int}
    — the element shape EventController users[] validation expects
    (back/2542 renamed the array key from `participants` to `users`;
    element shape unchanged). CLI flag `--participant` keeps its
    human-facing name; only the wire key changed.
    """
    from .contract import ROLE_QA, ROLE_REVIEWER

    if not spec or not isinstance(spec, str):
        raise ValueError(f"--participant must be a non-empty string, got: {spec!r}")
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(
            f"--participant must be 'type:id:role' (e.g. agent:5:reviewer), got: {spec!r}"
        )
    ptype, pid, role, *rest = parts
    if ptype not in ("agent", "user"):
        raise ValueError(f"--participant type must be 'agent' or 'user', got: {ptype!r}")
    try:
        value = int(pid)
    except ValueError as e:
        raise ValueError(f"--participant id must be int, got: {pid!r}") from e

    role_map = {"reviewer": ROLE_REVIEWER, "qa": ROLE_QA}
    if role in role_map:
        role_id = role_map[role]
    elif role == "role":
        if not rest:
            raise ValueError(
                f"--participant 'role:NUMBER' needs a number, got: {spec!r}"
            )
        try:
            role_id = int(rest[0])
        except ValueError as e:
            raise ValueError(
                f"--participant role id must be int, got: {rest[0]!r}"
            ) from e
    else:
        raise ValueError(
            f"--participant unknown role {role!r}; expected 'reviewer', 'qa', "
            f"or 'role:NUMBER'"
        )
    return {
        "participable_id": value,
        "type": ptype,
        "role_id": role_id,
    }


def _parse_tags(spec: str) -> list[dict[str, Any]]:
    """Parse the `--tags` sugar into settings.tags[] elements.

    Comma-separated list; each item is `key[:label[:color]]`:
      backend,urgent            -> [{key:backend}, {key:urgent}]
      backend:Backend:#ff0000   -> [{key:backend, label:Backend, color:#ff0000}]
      docs::#0af                -> [{key:docs, color:#0af}]   (empty label skipped)

    Returns the {key, label?, color?} dicts the create-contract-event tool
    persists under events.settings.tags[].
    """
    tags: list[dict[str, Any]] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        key, _, tail = item.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"--tags item needs a key, got: {raw!r}")
        tag: dict[str, Any] = {"key": key}
        if tail:
            label, _, color = tail.partition(":")
            label = label.strip()
            color = color.strip()
            if label:
                tag["label"] = label
            if color:
                tag["color"] = color
        tags.append(tag)
    if not tags:
        raise ValueError(f"--tags produced no tags from: {spec!r}")
    return tags


def _contract_dump(value: Any) -> None:
    if isinstance(value, str):
        sys.stdout.write(value)
        sys.stdout.write("\n")
    else:
        json.dump(value, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    sys.stdout.flush()


def _cmd_contract(args: argparse.Namespace) -> int:
    from .contract import ContractError, contract_ids_from_env

    action = args.contract_action
    try:
        with _contract_client() as cli:
            if action == "list":
                _contract_dump(cli.list_contracts())
            elif action == "members":
                _contract_dump(cli.members(args.cid))
            elif action == "tasks":
                ids = [args.cid] if args.cid is not None else contract_ids_from_env()
                if not ids:
                    _err("no contract id (positional or CEKI_CONTRACT_IDS)", "args")
                    return 1
                for cid in ids:
                    print(f"--- contract {cid} ---")
                    _contract_dump(cli.tasks(int(cid)))
            elif action == "my-events":
                _contract_dump(cli.my_events())
            elif action == "my-jobs":
                print("⚠️  [DEPRECATED] use `ceki hire my-jobs` instead", file=sys.stderr)
                _contract_dump(cli.my_jobs())
            elif action == "call-human":
                _contract_dump(cli.call_human(args.event_id, args.kind, args.desc))
            elif action == "task":
                _contract_dump(cli.task(args.eid))
            elif action == "children":
                _contract_dump(cli.children(args.eid))
            elif action == "history":
                _contract_dump(cli.history(args.eid, limit=args.limit))
            elif action == "create":
                cid = args.cid if args.cid is not None else (
                    int(contract_ids_from_env()[0]) if contract_ids_from_env() else None
                )
                if cid is None:
                    _err("contract id required (positional or CEKI_CONTRACT_IDS)", "args")
                    return 1
                data_obj = json.loads(args.data) if args.data else None
                try:
                    extra_parts = [
                        _parse_participant(spec)
                        for spec in (getattr(args, "participant", None) or [])
                    ]
                    tags = _parse_tags(args.tags) if getattr(args, "tags", None) else None
                except ValueError as e:
                    _err(str(e), "args")
                    return 1
                _contract_dump(cli.create(
                    cid,
                    label=args.label,
                    type_id=args.type,
                    status_id=args.status,
                    kal_schedule_id=args.kal_schedule,
                    start=args.start,
                    end=args.end,
                    timezone=args.timezone,
                    date=args.date,
                    duration=args.duration,
                    amount=args.amount,
                    currency=args.currency,
                    description=args.desc,
                    data=data_obj,
                    benefitable=args.benefitable,
                    reviewer=args.reviewer,
                    qa=args.qa,
                    participants=extra_parts or None,
                    tags=tags,
                ))
            elif action == "comment":
                # `--label` → label (short header), `--desc` → description
                # (long body). When only one is given it goes to label for
                # backward compatibility; when both are given desc feeds the
                # description field.
                label = args.label
                if label is not None:
                    # --label given: use --desc as description
                    description = args.desc
                else:
                    # Only --desc (or neither): body goes to label
                    label = args.desc
                    description = None
                if label is None:
                    _contract_dump({"error": "provide --label or --desc"})
                    return
                _contract_dump(cli.comment(
                    args.eid,
                    label=label,
                    description=description,
                    type_id=args.type,
                    status_id=args.status,
                    start=args.start,
                    end=args.end,
                    date=args.date,
                    duration=args.duration,
                    amount=args.amount,
                    currency=args.currency,
                    benefitable=args.benefitable,
                ))
            elif action == "propose":
                tags = _parse_tags(args.tags) if getattr(args, "tags", None) else None
                settings: dict[str, Any] | None = (
                    {"tags": tags} if tags else None
                )
                _contract_dump(cli.propose(
                    args.eid,
                    status_id=args.status,
                    label=args.label,
                    description=args.desc,
                    start=args.start,
                    end=args.end,
                    date=args.date,
                    duration=args.duration,
                    amount=args.amount,
                    currency=args.currency,
                    benefitable=args.benefitable,
                    settings=settings,
                ))
            elif action == "progress":
                _contract_dump(cli.progress(
                    args.eid,
                    status=args.status,
                    desc=args.desc,
                ))
            elif action == "vote":
                ids = [int(s) for s in str(args.ids).split(",") if s.strip()]
                vote = str(args.vote).lower() in ("true", "1", "yes")
                _contract_dump(cli.vote(args.eid, ids, vote))
            elif action == "poll":
                items = cli.poll()
                _contract_dump({"count": len(items), "notifications": items})
            elif action == "watch":
                sec = max(6, int(args.interval or 8))
                sys.stderr.write(
                    f"[watch] poll every {sec}s (limit 10/min/token; do not go below 6s)\n"
                )
                sys.stderr.flush()
                import time as _time
                while True:
                    items = cli.poll()
                    if items:
                        from datetime import datetime, timezone
                        ts = datetime.now(timezone.utc).isoformat()
                        for n in items:
                            print(json.dumps({"ts": ts, "notification": n}, ensure_ascii=False))
                    _time.sleep(sec)
            elif action == "tools":
                _contract_dump(cli.tools())
            elif action == "raw":
                payload = json.loads(args.args) if args.args else {}
                _contract_dump(cli.raw(args.tool, payload))
            else:
                _err(f"unknown contract action: {action}")
                return 1
    except ContractError as e:
        _err(str(e), "contract")
        return 1
    return 0


def _cmd_hire(args: argparse.Namespace) -> int:

    action = args.hire_action
    try:
        with _contract_client() as cli:
            if action == "my-jobs":
                _contract_dump(cli.my_jobs())
        return 0
    except Exception as e:
        _err(str(e), "error")
        return 1


def _cmd_timelog(args: argparse.Namespace) -> int:
    from .contract import ContractError
    from .timelog import TimelogClient

    action = args.timelog_action
    try:
        with TimelogClient() as cli:
            if action == "start":
                _contract_dump(cli.start(args.event_id))
            elif action == "stop":
                _contract_dump(cli.stop(args.event_id, label=args.label))
            elif action == "check":
                _contract_dump(cli.check(args.event_id))
            else:
                _err(f"unknown timelog action: {action}")
                return 1
    except ContractError as e:
        _err(str(e), "timelog")
        return 1
    return 0


async def _cmd_cdp(args: argparse.Namespace) -> None:
    # Try daemon IPC
    params = json.loads(args.params) if args.params else {}
    try:
        result = await _daemon_request("/cdp", {
            "session_id": args.session_id,
            "method": args.method,
            "params": params,
        })
        if result is not None:
            _out(result)
            return
    except CekiError as e:
        _err(str(e), "daemon")
        sys.exit(6)

    # Fallback to one-shot
    api_key = _get_api_key()
    client, browser = await _resume_browser(api_key, args.session_id)
    try:
        result = await browser.send({"method": args.method, "params": params})
        _out(result)
    finally:
        if client._ws:
            await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ceki", description="CLI for browser.ceki.me rental")
    parser.add_argument(
        "--no-human", "--raw",
        action="store_true",
        dest="no_human",
        help="Disable behavioral humanization (mouse jitter, typing cadence) "
             "for this command. Same as CEKI_HUMAN_DISABLE=1 but per-call.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_rent = sub.add_parser("rent", help="Rent a browser")
    p_rent.add_argument("--schedule", type=int, required=True, help="Schedule ID")
    p_rent.add_argument(
        "--mode", choices=["incognito", "main"],
        default="incognito", help="Profile mode (default: incognito)",
    )
    p_rent.add_argument("--fingerprint-from", help="Path to profile JSON with fingerprint data")

    p_snap = sub.add_parser("snapshot", help="Take screenshot + get new chat messages")
    p_snap.add_argument("session_id", help="Session ID")
    p_snap.add_argument("-o", "--output", required=True, help="Output PNG path")

    p_nav = sub.add_parser("navigate", help="Navigate to URL")
    p_nav.add_argument("session_id", help="Session ID")
    p_nav.add_argument("url", help="URL to navigate to")
    p_nav.add_argument("--no-human", "--raw", action="store_true", dest="no_human",
                       help="Skip humanization for this call")

    p_click = sub.add_parser("click", help="Click at coordinates")
    p_click.add_argument("session_id", help="Session ID")
    p_click.add_argument("x", type=int, help="X coordinate")
    p_click.add_argument("y", type=int, help="Y coordinate")
    p_click.add_argument("--no-human", "--raw", action="store_true", dest="no_human",
                         help="Skip humanization (mouse jitter) for this call")

    p_type = sub.add_parser("type", help="Type text")
    p_type.add_argument("session_id", help="Session ID")
    p_type.add_argument("text", help="Text to type")
    # task 429 — typing is humanized BY DEFAULT in both modes (revert of
    # 428 opt-in). --no-human / --raw explicitly flattens THIS call only.
    # --natural is a no-op alias kept for backwards compatibility.
    p_type.add_argument("--natural", action="store_true",
                        help=argparse.SUPPRESS)
    p_type.add_argument("--no-human", "--raw", action="store_true", dest="no_human",
                        help="Skip humanization (typing cadence) for this call")
    p_type.add_argument(
        "--selector",
        help="CSS selector to focus before typing (e.g. 'input[type=email]')",
    )

    p_scroll = sub.add_parser("scroll", help="Scroll")
    p_scroll.add_argument("session_id", help="Session ID")
    p_scroll.add_argument("x", type=int, help="X origin")
    p_scroll.add_argument("y", type=int, help="Y origin")
    p_scroll.add_argument("dy", type=int, help="Delta Y (negative = scroll down)")
    p_scroll.add_argument("--no-human", "--raw", action="store_true", dest="no_human",
                          help="Skip humanization for this call")

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
    p_upload.add_argument(
        "--mime", dest="mime_type",
        help="Override MIME type (default: auto-detect from extension)",
    )

    p_captcha = sub.add_parser("request-captcha", help="Request human to solve captcha")
    p_captcha.add_argument("session_id", help="Session ID")
    p_captcha.add_argument(
        "--acceptance", type=float, default=60,
        help="Acceptance timeout sec (min 30)",
    )
    p_captcha.add_argument(
        "--completion", type=float, default=120,
        help="Completion timeout sec (min 30)",
    )
    p_captcha.add_argument(
        "--manual", action="store_true",
        help="Disable auto-accept (agent votes manually)",
    )

    p_cdp = sub.add_parser("cdp", help="Send raw CDP command")
    p_cdp.add_argument("session_id", help="Session ID")
    p_cdp.add_argument("--method", required=True, help="CDP method name")
    p_cdp.add_argument("--params", help="CDP params as JSON string")

    p_contract = sub.add_parser("contract", help="Participate in contracts via /mcp/agent")
    csub = p_contract.add_subparsers(dest="contract_action", required=True)

    p_hire = sub.add_parser("hire", help="Hire schedule commands (my-jobs)")
    hsub = p_hire.add_subparsers(dest="hire_action", required=True)

    hsub.add_parser(
        "my-jobs",
        help=(
            "List hire schedules I posted, type 3 (get-my-jobs). "
            "The listings feed."
        ),
    )

    csub.add_parser("list", help="List my contracts (get-my-contracts)")

    p_cm = csub.add_parser("members", help="List contract members")
    p_cm.add_argument("cid", type=int, help="Contract ID")

    p_ct = csub.add_parser("tasks", help="List contract events (default: CEKI_CONTRACT_IDS)")
    p_ct.add_argument("cid", type=int, nargs="?", help="Contract ID")

    csub.add_parser(
        "my-events",
        help=(
            "List contract events assigned to me (get-my-events). "
            "The 'plate' feed. Wire tool renamed from get-my-jobs."
        ),
    )
    csub.add_parser(
        "my-jobs",
        help=(
            "List hire schedules I posted, type 3 (get-my-jobs). "
            "The listings feed. Wire tool reused after the backend swap "
            "(formerly get-hire-jobs); for contract events use 'my-events'."
        ),
    )

    p_ctask = csub.add_parser("task", help="Get event")
    p_ctask.add_argument("eid", type=int, help="Event ID")

    p_cch = csub.add_parser("children", help="Get event children")
    p_cch.add_argument("eid", type=int, help="Event ID")

    p_chist = csub.add_parser("history", help="Get event audit history")
    p_chist.add_argument("eid", type=int, help="Event ID")
    p_chist.add_argument("--limit", type=int, help="Max entries")

    p_cc = csub.add_parser("create", help="Create contract event")
    p_cc.add_argument(
        "cid",
        type=int,
        nargs="?",
        help="Contract ID (default: CEKI_CONTRACT_IDS[0])",
    )
    p_cc.add_argument("--label", required=True)
    p_cc.add_argument("--type", type=int)
    p_cc.add_argument("--status", type=int)
    p_cc.add_argument("--kal-schedule", type=int, dest="kal_schedule")
    p_cc.add_argument("--start")
    p_cc.add_argument("--end")
    p_cc.add_argument("--timezone", help="IANA tz (e.g. Europe/Moscow)")
    p_cc.add_argument("--date")
    p_cc.add_argument("--duration", type=int)
    p_cc.add_argument("--amount", type=int)
    p_cc.add_argument("--currency")
    p_cc.add_argument("--benefitable", help="agent:8 or user:61")
    p_cc.add_argument("--reviewer", help="agent:8 or user:61 (role_id 5 shortcut)")
    p_cc.add_argument("--qa", help="agent:8 or user:61 (role_id 6 shortcut)")
    p_cc.add_argument(
        "--participant",
        action="append",
        default=[],
        dest="participant",
        help=(
            "Repeatable. agent:N:reviewer | user:N:qa | agent:N:role:NUMBER. "
            "Stacks on top of --reviewer/--qa."
        ),
    )
    p_cc.add_argument("--desc")
    p_cc.add_argument("--data", help="Extra JSON object passed through as `data`")
    p_cc.add_argument(
        "--tags",
        help=(
            "Project tags (sugar for settings.tags[]). Comma-separated, each "
            "item key[:label[:color]]. E.g. 'backend,urgent' or "
            "'backend:Backend:#ff0000'."
        ),
    )

    p_cco = csub.add_parser("comment", help="Post comment on event")
    p_cco.add_argument("eid", type=int)
    p_cco.add_argument("--label")
    p_cco.add_argument("--type", type=int)
    p_cco.add_argument("--status", type=int)
    p_cco.add_argument("--start")
    p_cco.add_argument("--end")
    p_cco.add_argument("--date")
    p_cco.add_argument("--duration", type=int)
    p_cco.add_argument("--amount", type=int)
    p_cco.add_argument("--currency")
    p_cco.add_argument("--benefitable")
    p_cco.add_argument("--desc")

    p_cp = csub.add_parser("propose", help="Propose correction")
    p_cp.add_argument("eid", type=int)
    p_cp.add_argument("--status", type=int)
    p_cp.add_argument("--label")
    p_cp.add_argument("--desc")
    p_cp.add_argument("--start")
    p_cp.add_argument("--end")
    p_cp.add_argument("--date")
    p_cp.add_argument("--duration", type=int)
    p_cp.add_argument("--amount", type=int)
    p_cp.add_argument("--currency")
    p_cp.add_argument("--benefitable")
    p_cp.add_argument(
        "--tags",
        help=(
            "Project tags (sugar for settings.tags[]). Comma-separated, each "
            "item key[:label[:color]]. E.g. 'backend,urgent' or "
            "'backend:Backend:#ff0000'. back/2796 persists onto the event."
        ),
    )

    p_cpr = csub.add_parser(
        "progress",
        help="Status correction + progress comment (description is not touched)",
    )
    p_cpr.add_argument("eid", type=int)
    p_cpr.add_argument("--status", type=int)
    p_cpr.add_argument("--desc", required=True)

    p_cv = csub.add_parser("vote", help="Vote on correction(s)")
    p_cv.add_argument("eid", type=int)
    p_cv.add_argument("--ids", required=True, help="Comma-separated correction IDs")
    p_cv.add_argument("--vote", required=True, help="true|false")

    csub.add_parser("poll", help="Single agent polling tick")

    p_cw = csub.add_parser("watch", help="Continuous polling")
    p_cw.add_argument("interval", type=int, nargs="?", default=8, help="Seconds, min 6")

    p_ch = csub.add_parser(
        "call-human",
        help="Escalate to a human on an event (input/review/stuck).",
    )
    p_ch.add_argument("event_id", type=int)
    p_ch.add_argument(
        "--kind",
        choices=["input", "review", "stuck"],
        required=True,
        help="Type of escalation: input | review | stuck.",
    )
    p_ch.add_argument(
        "--desc",
        required=True,
        help="Specific question / decision / what was tried.",
    )

    csub.add_parser("tools", help="List available MCP tools")

    p_craw = csub.add_parser("raw", help="Call raw MCP tool")
    p_craw.add_argument("tool")
    p_craw.add_argument("args", nargs="?", default="{}", help="JSON args")

    p_timelog = sub.add_parser(
        "timelog", help="Time-tracking for events via /mcp/agent (start/stop/check)"
    )
    tlsub = p_timelog.add_subparsers(dest="timelog_action", required=True)

    p_tls = tlsub.add_parser("start", help="Open timelog for event_id (timelog-start)")
    p_tls.add_argument("event_id", type=int, help="Event ID")

    p_tlp = tlsub.add_parser(
        "stop", help="Close open timelog for event_id (timelog-stop); duration computed server-side"
    )
    p_tlp.add_argument("event_id", type=int, help="Event ID")
    p_tlp.add_argument("--label", help="Label for the closing child event (e.g. 'что сделал')")

    p_tlc = tlsub.add_parser(
        "check", help="Check whether an open timelog exists for event_id (timelog-check)"
    )
    p_tlc.add_argument("event_id", type=int, help="Event ID")

    # ── daemon subcommand ─────────────────────────────────────────────
    p_daemon = sub.add_parser("daemon", help="Manage persistent renter daemon")
    dsub = p_daemon.add_subparsers(dest="daemon_action", required=True)
    dsub.add_parser("start", help="Start daemon (detached subprocess)")
    dsub.add_parser("stop", help="Stop daemon (SIGTERM)")
    dsub.add_parser("status", help="Check daemon status")

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

    if args.command == "contract":
        sys.exit(_cmd_contract(args))

    if args.command == "hire":
        sys.exit(_cmd_hire(args))

    if args.command == "timelog":
        sys.exit(_cmd_timelog(args))

    if args.command == "daemon":
        sys.exit(_cmd_daemon(args))

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

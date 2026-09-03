"""ceki-daemon — persistent renter-process for browser.ceki.me.

Maintains persistent WebSocket sessions to the relay, exposing them via a
local HTTP/JSON IPC server. CLI commands route through the daemon when it is
running, avoiding the one-shot disconnect → ``no_session`` cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import ConnectOptions, connect
from ._browser import _unwrap_screenshot_data
from ._exceptions import ConnectionLost, SessionNotFound

log = logging.getLogger(__name__)

DAEMON_HOST = "127.0.0.1"
PID_FILE = Path("/tmp/ceki-daemon.pid")


def daemon_port() -> int:
    return int(os.environ.get("CEKI_DAEMON_PORT", "18777"))


def _connect_options() -> ConnectOptions:
    opts = ConnectOptions(reconnect=True)
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


def is_running() -> bool:
    """Check if daemon is running via PID file + health check."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        if pid <= 0:
            PID_FILE.unlink(missing_ok=True)
            return False
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return False
    try:
        import urllib.request as _ureq

        port = daemon_port()
        req = _ureq.Request(f"http://{DAEMON_HOST}:{port}/health")
        with _ureq.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("ok") is True
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# HTTP handler  (runs in a thread, bridges to the asyncio event-loop)
# ──────────────────────────────────────────────────────────────────────────────


class DaemonHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for daemon IPC requests.

    Each POST endpoint maps to a private ``_handle_*`` coroutine.  The handler
    calls ``self.server.daemon_server.run_async(coro)`` to execute the coroutine on the
    daemon's asyncio event-loop (running in the main thread) and returns the
    JSON result to the caller.
    """

    server: DaemonServer  # type: ignore[assignment]

    # Silence per-request log lines (class-wide stderr redirect handled
    # via log_message override below).
    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("HTTP %s", fmt % args)

    # ── helpers ────────────────────────────────────────────────────────

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── HTTP methods ───────────────────────────────────────────────────

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True, "pid": os.getpid()})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            params: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json(400, {"ok": False, "error": f"invalid JSON: {e}"})
            return

        path = self.path.rstrip("/")
        handler_name = _ENDPOINTS.get(path)
        if handler_name is None:
            self._send_json(404, {"ok": False, "error": f"unknown endpoint: {path}"})
            return

        handler = getattr(self, handler_name)
        coro = handler(params)
        try:
            result = self.server.daemon_server.run_async(coro)
            self._send_json(200, {"ok": True, "result": result})
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except SessionNotFound as e:
            self._send_json(404, {"ok": False, "error": str(e)})
        except asyncio.TimeoutError:
            self._send_json(504, {"ok": False, "error": "timeout"})
        except Exception as e:
            log.error("handler error on %s: %s", path, e, exc_info=True)
            self._send_json(500, {"ok": False, "error": str(e)})

    # ── Endpoint handlers ──────────────────────────────────────────────

    async def _handle_rent(self, params: dict) -> dict:
        """``POST /rent`` — rent a new browser session."""
        api_key = params.get("api_key") or os.environ.get("CEKI_API_KEY")
        if not api_key:
            raise ValueError("CEKI_API_KEY not set")
        schedule = params.get("schedule")
        if not schedule:
            raise ValueError("schedule (int) required")
        mode = params.get("mode", "incognito")
        fp_data: bool | dict = True
        fp_from = params.get("fingerprint_from")
        if fp_from:
            with open(fp_from) as f:
                profile = json.load(f)
            fp_data = profile.get("fingerprint") or True

        # Reuse ONE shared Client per api_key — all sessions multiplex over a
        # single WebSocket.  Old per-rent clients were never closed, leaking a
        # live WS per rent and confusing relay cdp_response routing.
        daemon = self.server.daemon_server

        async def _shared_client():
            client = daemon._clients.get(api_key)
            # A cached client may be closed already (WS torn down after the
            # last session ended, or by a failed rent) — never reuse it.
            if client is None or client._closed or client._ws is None:
                client = await connect(api_key, _connect_options())
                client._on_session_ended = daemon._on_session_ended
                daemon._clients[api_key] = client
            return client

        client = await _shared_client()
        try:
            browser = await client.rent(schedule, mode=mode, fingerprint=fp_data)
        except (TimeoutError, ConnectionLost) as exc:
            # The shared WS is half-dead: the relay stopped routing rent/match
            # without a close frame, so the TCP socket stays ESTABLISHED,
            # recv() never raises and pongs keep coming — neither the reader
            # nor the heartbeat notices.  Every rent through it hangs 90s and
            # 504s forever until the daemon restarts.  Drop the poisoned client
            # and retry ONCE on a fresh connection.
            log.warning(
                "rent failed for %s (%s) — recreating shared client",
                api_key, type(exc).__name__,
            )
            await daemon._drop_client(api_key, client)
            client = await _shared_client()
            try:
                browser = await client.rent(schedule, mode=mode, fingerprint=fp_data)
            except Exception:
                await daemon._drop_client(api_key, client)
                raise
        except Exception:
            if not daemon._client_has_sessions(client):
                if daemon._clients.get(api_key) is client:
                    daemon._clients.pop(api_key, None)
                try:
                    await client.disconnect()
                except Exception:
                    pass
            raise
        daemon._sessions[browser.session_id] = browser
        return {
            "session_id": browser.session_id,
            "chat_topic_id": browser.chat_topic_id,
            "schedule_id": browser.schedule_id,
        }

    async def _handle_navigate(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        human = params.get("human")  # None → use default, False → raw
        await browser.navigate(params["url"], human=human)

    async def _handle_click(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        human = params.get("human")
        await browser.click(int(params["x"]), int(params["y"]), human=human)

    async def _handle_type(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        human = params.get("human")
        await browser.type(
            params["text"],
            selector=params.get("selector"),
            human=human,
        )

    async def _handle_scroll(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        human = params.get("human")
        await browser.scroll(
            params.get("x", 0),
            params.get("y", 0),
            delta_x=params.get("dx", 0),
            delta_y=params.get("dy", -300),
            human=human,
        )

    async def _handle_switch_tab(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        await browser.switch_tab()

    async def _handle_configure(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        kwargs: dict[str, Any] = {}
        if params.get("masking_mode") is not None:
            kwargs["masking_mode"] = params["masking_mode"]
        if params.get("fingerprint") is not None:
            kwargs["fingerprint"] = params["fingerprint"]
        await browser.configure(**kwargs)

    async def _handle_screenshot(self, params: dict) -> dict:
        browser = await self._resolve_browser(params)
        full = params.get("full", False)
        resp = await browser.screenshot(format="base64", full_page=full)
        data = _unwrap_screenshot_data(resp) if isinstance(resp, dict) else resp
        return {"data": data}

    async def _handle_snapshot(self, params: dict) -> dict:
        browser = await self._resolve_browser(params)
        snap = await browser.snapshot()
        chat_list = [
            {"from": m.sender_id, "text": m.text, "ts": m.created_at}
            for m in snap.chat
        ]
        return {
            "screenshot": snap.screenshot or "",
            "chat": chat_list,
            "ts": snap.ts.isoformat(),
        }

    async def _handle_stop(self, params: dict) -> None:
        session_id = params.get("session_id", "")
        browser = self.server.daemon_server._sessions.pop(session_id, None)
        if browser is None:
            raise ValueError(f"session not found: {session_id}")
        try:
            await browser.close()
        finally:
            # Shared client is disconnected only when the LAST session ends.
            await self.server.daemon_server._maybe_disconnect_clients()

    async def _handle_chat_send(self, params: dict) -> dict:
        browser = await self._resolve_browser(params)
        result = await browser.chat.send(params["text"])
        return {"message_id": result.get("message_id")}

    async def _handle_chat_next(self, params: dict) -> dict | None:
        browser = await self._resolve_browser(params)
        timeout = params.get("timeout", 60)
        since = params.get("since") or browser._last_seen_ts
        msgs = await browser.chat.history(since=since)
        if msgs:
            m = msgs[0]
            browser._last_seen_ts = m.created_at
            return {"from": m.sender_id, "text": m.text, "ts": m.created_at}
        got = asyncio.Event()
        result: dict = {}

        async def on_msg(msg):
            result["from"] = msg.sender_id
            result["text"] = msg.text
            result["ts"] = msg.created_at
            got.set()

        browser.chat.on_message(on_msg)
        try:
            await asyncio.wait_for(got.wait(), timeout=timeout)
            browser._last_seen_ts = result["ts"]
            return result
        except asyncio.TimeoutError:
            return None

    async def _handle_chat_history(self, params: dict) -> list[dict]:
        browser = await self._resolve_browser(params)
        since = params.get("since")
        limit = params.get("limit", 50)
        msgs = await browser.chat.history(since=since, limit=limit)
        return [
            {"from": m.sender_id, "text": m.text, "ts": m.created_at}
            for m in msgs
        ]

    async def _handle_cdp(self, params: dict) -> Any:
        browser = await self._resolve_browser(params)
        method = params["method"]
        cdp_params = params.get("params", {})
        return await browser.send({"method": method, "params": cdp_params})

    async def _handle_profile_export(self, params: dict) -> dict:
        browser = await self._resolve_browser(params)
        domains = None
        if params.get("domains"):
            domains = [d.strip() for d in params["domains"].split(",")]
        include_session = not params.get("no_session_storage", False)
        profile = await browser.profile.export(
            domains=domains,
            include_session_storage=include_session,
        )
        return profile

    async def _handle_profile_import(self, params: dict) -> None:
        browser = await self._resolve_browser(params)
        profile = params.get("profile")
        if not profile:
            raise ValueError("profile data required")
        await browser.profile.import_(profile)

    async def _handle_upload(self, params: dict) -> dict:
        browser = await self._resolve_browser(params)
        selector = params.get("selector", "")
        if not selector:
            raise ValueError("selector required")
        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("file_path required")
        filename = params.get("filename")
        mime_type = params.get("mime_type")
        result = await browser.upload(
            selector,
            file_path,
            filename=filename,
            mime_type=mime_type,
        )
        return result

    async def _handle_request_captcha(self, params: dict) -> dict:
        browser = await self._resolve_browser(params)
        auto = not params.get("manual", False)
        result = await browser.request_captcha(
            acceptance_timeout=params.get("acceptance", 60),
            completion_timeout=params.get("completion", 120),
            auto_accept=auto,
        )
        return result.to_dict()

    # ── session resolution ─────────────────────────────────────────────

    async def _resolve_browser(self, params: dict):
        """Look up a stored Browser by session_id."""
        session_id = params.get("session_id", "")
        if not session_id:
            raise ValueError("session_id required")
        browser = self.server.daemon_server._sessions.get(session_id)
        if browser is None:
            raise SessionNotFound(f"session not found: {session_id}")
        return browser


_ENDPOINTS: dict[str, str] = {
    "/rent": "_handle_rent",
    "/navigate": "_handle_navigate",
    "/click": "_handle_click",
    "/type": "_handle_type",
    "/scroll": "_handle_scroll",
    "/switch-tab": "_handle_switch_tab",
    "/configure": "_handle_configure",
    "/screenshot": "_handle_screenshot",
    "/snapshot": "_handle_snapshot",
    "/stop": "_handle_stop",
    "/chat/send": "_handle_chat_send",
    "/chat/next": "_handle_chat_next",
    "/chat/history": "_handle_chat_history",
    "/cdp": "_handle_cdp",
    "/profile/export": "_handle_profile_export",
    "/profile/import": "_handle_profile_import",
    "/upload": "_handle_upload",
    "/request-captcha": "_handle_request_captcha",
}


# ──────────────────────────────────────────────────────────────────────────────
# Daemon server  (asyncio event-loop in main thread, HTTP in daemon thread)
# ──────────────────────────────────────────────────────────────────────────────


class DaemonServer:
    """HTTP/JSON IPC server maintaining persistent browser sessions.

    Architecture
    ------------
    - Main thread runs an **asyncio** event-loop that owns all WS connections
      and ``Browser`` objects.
    - A daemon thread runs a ``ThreadingHTTPServer`` that accepts IPC requests.
    - The HTTP handler calls :meth:`run_async` to schedule a coroutine on the
      event-loop and waits for its result — bridging sync → async boundaries.
    - Sessions are stored in memory as ``{session_id: Browser}``.
    - Clients are shared per ``api_key`` in ``{api_key: Client}`` — all sessions
      of one key multiplex over a single WebSocket, so there is never more than
      one live connection per key.
    - ``SIGTERM`` / ``SIGINT`` triggers a graceful shutdown: all sessions are
      closed, shared clients are disconnected, the PID file is removed, and the
      event-loop stops.
    """

    def __init__(self, host: str = DAEMON_HOST, port: int | None = None) -> None:
        self.host = host
        self.port = port or daemon_port()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, Any] = {}
        self._clients: dict[str, Any] = {}

    # ── public API ─────────────────────────────────────────────────────

    def run_async(self, coro: Any, timeout: float = 300.0) -> Any:
        """Schedule *coro* on the event-loop from a sync thread and wait."""
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("daemon event-loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except asyncio.TimeoutError:
            fut.cancel()
            raise

    def start(self) -> None:
        """Start the daemon (blocking — runs until shutdown)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Register signal handlers (main thread only)
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(
                sig, lambda: self._loop.create_task(self._shutdown()),
            )

        # HTTP server in a daemon thread
        self._httpd = ThreadingHTTPServer((self.host, self.port), DaemonHTTPHandler)
        self._httpd.daemon_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="http-server",
        )
        self._thread.start()

        # PID file
        PID_FILE.write_text(str(os.getpid()))
        log.info(
            "daemon started — %s:%d (pid %d)",
            self.host, self.port, os.getpid(),
        )

        try:
            self._loop.run_forever()
        finally:
            self._cleanup()

    # ── lifecycle ──────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        log.info("shutting down (closing %d session(s))", len(self._sessions))
        # Close all sessions
        for session_id, browser in list(self._sessions.items()):
            try:
                await browser.close()
            except Exception as exc:
                log.debug("close session %s: %s", session_id, exc, exc_info=True)
        self._sessions.clear()
        # Disconnect all shared clients
        for api_key, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        # Stop HTTP server (blocking call offloaded to thread pool)
        if self._httpd:
            await asyncio.to_thread(self._httpd.shutdown)
        self._loop.stop()

    def _client_has_sessions(self, client: Any) -> bool:
        """True if any live session belongs to *client* (via its Browser)."""
        return any(browser._client is client for browser in self._sessions.values())

    async def _disconnect_client(self, client: Any) -> None:
        try:
            await client.disconnect()
        except Exception as exc:
            log.debug("disconnect shared client: %s", exc, exc_info=True)

    async def _drop_client(self, api_key: str, client: Any) -> None:
        """Remove a poisoned shared client and drop its sessions.

        Called when a rent times out on a client whose WebSocket went half-dead
        (relay stopped routing without a close frame).  That WS can no longer
        carry rent/match messages, so the client is dropped from the cache and
        any sessions it owned are discarded — they are unreachable anyway.
        """
        if self._clients.get(api_key) is client:
            self._clients.pop(api_key, None)
        for sid in [
            sid for sid, browser in self._sessions.items()
            if browser._client is client
        ]:
            self._sessions.pop(sid, None)
        await self._disconnect_client(client)

    async def _maybe_disconnect_clients(self) -> None:
        """Disconnect shared clients once the last session for them is gone.

        Called from the HTTP handler (not the client's own reader task), so
        awaiting ``client.disconnect()`` here is safe.
        """
        if self._sessions:
            return
        clients = list(self._clients.items())
        self._clients.clear()
        for _, client in clients:
            await self._disconnect_client(client)

    async def _on_session_ended(self, session_id: str) -> None:
        """Daemon-side cleanup when a rented session ends on the relay.

        Invoked by the shared :class:`Client` on ``session.ended``/``session_end``
        (see ``_client.py``).  Removes the session from ``_sessions`` and, when
        the last session is gone, closes the shared client's WebSocket so the
        relay never accumulates orphan connections.

        This runs inside the client's own reader task, so disconnecting the
        client must be deferred to a separate task — ``disconnect()`` cancels
        the reader task, which would otherwise cancel this very coroutine and
        skip the actual WS/P2P teardown.
        """
        self._sessions.pop(session_id, None)
        if self._sessions:
            return
        clients = list(self._clients.items())
        self._clients.clear()
        for _, client in clients:
            # Stop the reader loop synchronously; teardown happens in a task.
            client._closed = True
            asyncio.create_task(self._disconnect_client(client))

    def _cleanup(self) -> None:
        PID_FILE.unlink(missing_ok=True)
        log.info("daemon stopped")


# ── entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = DaemonServer()
    try:
        server.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

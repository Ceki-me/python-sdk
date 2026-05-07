from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import websockets
import websockets.exceptions

from ._browser import Browser
from ._exceptions import (
    AuthFailed,
    CdpUnrecoverable,
    ConnectionLost,
    InsufficientFunds,
    RateLimitExceeded,
    SessionEnded,
)
from ._models import BrowserOption, Match

log = logging.getLogger(__name__)

BACKOFF_STEPS = [1, 2, 4, 8, 16, 32, 60]
MAX_RECONNECT_ATTEMPTS = 10
HEARTBEAT_INTERVAL = 30.0
HEARTBEAT_TIMEOUT = 90.0


class Client:
    def __init__(
        self,
        api_key: str,
        relay_url: str,
        api_url: str,
        reconnect: bool = True,
        basic_auth: tuple[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.relay_url = relay_url
        self.api_url = api_url
        self.reconnect = reconnect
        self._basic_auth = basic_auth
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending_rents: dict[str, asyncio.Future[Match]] = {}
        self._pending_rent_queue: list[asyncio.Future[Match]] = []
        self._active_browsers: dict[str, Browser] = {}
        self._backoff_attempt = 0
        self._last_pong = 0.0
        self._closed = False

    def _ws_extra_headers(self) -> dict[str, str]:
        if not self._basic_auth:
            return {}
        import base64
        creds = base64.b64encode(
            f"{self._basic_auth[0]}:{self._basic_auth[1]}".encode()
        ).decode()
        return {"Authorization": f"Basic {creds}"}

    async def _connect(self) -> None:
        subprotocols = [f"bearer.{self.api_key}"]
        extra_headers = self._ws_extra_headers()
        try:
            self._ws = await websockets.connect(
                self.relay_url,
                subprotocols=subprotocols,  # type: ignore[arg-type]
                extra_headers=extra_headers,
                open_timeout=20,
            )
        except websockets.exceptions.InvalidStatusCode as exc:
            if exc.status_code in (401, 403):
                raise AuthFailed(f"handshake rejected: {exc.status_code}") from exc
            raise
        self._last_pong = time.monotonic()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        self._reader_task = asyncio.create_task(self._reader_loop(), name="reader")
        log.info("connected to relay %s", self.relay_url)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def search(
        self, filters: dict[str, Any] | None = None, limit: int = 20
    ) -> list[BrowserOption]:
        url = f"{self.api_url}/api/browsers/search"
        params: dict[str, Any] = {"limit": limit, **(filters or {})}
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                params=params,
            )
            resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        return [BrowserOption.model_validate(x) for x in items]

    async def rent(self, schedule_id: int) -> Browser:
        fut: asyncio.Future[Match] = asyncio.get_event_loop().create_future()
        self._pending_rent_queue.append(fut)
        await self._ws_send({"type": "rent", "schedule_id": schedule_id})
        try:
            match = await asyncio.wait_for(fut, timeout=60)
        except asyncio.TimeoutError:
            try:
                self._pending_rent_queue.remove(fut)
            except ValueError:
                pass
            raise TimeoutError("rent timed out waiting for match")
        browser = Browser(client=self, match=match)
        self._active_browsers[match.session_id] = browser
        return browser

    async def close(self) -> None:
        self._closed = True
        for browser in list(self._active_browsers.values()):
            await browser.close()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _ws_send(self, msg: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionLost("no websocket connection")
        await self._ws.send(json.dumps(msg))

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self._closed:
                break
            try:
                await self._ws_send({"type": "ping"})
            except Exception:
                break
            if time.monotonic() - self._last_pong > HEARTBEAT_TIMEOUT:
                log.warning("heartbeat timeout, forcing reconnect")
                if self._ws:
                    await self._ws.close()
                break

    async def _reader_loop(self) -> None:
        while not self._closed:
            try:
                assert self._ws is not None
                raw = await self._ws.recv()
                msg: dict[str, Any] = json.loads(raw)
                await self._dispatch(msg)
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
            ):
                if not self._closed and self.reconnect:
                    asyncio.create_task(self._reconnect_loop())
                else:
                    self._fail_pending(ConnectionLost("connection closed"))
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("reader error: %s", exc)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "pong":
            self._last_pong = time.monotonic()
            return
        if mtype == "rent_pending":
            server_event_id = msg.get("event_id")
            if self._pending_rent_queue and server_event_id:
                fut = self._pending_rent_queue.pop(0)
                if not fut.done():
                    self._pending_rents[str(server_event_id)] = fut
            return
        if mtype == "match":
            server_event_id = str(msg.get("event_id", ""))
            fut = self._pending_rents.pop(server_event_id, None)
            if fut and not fut.done():
                fut.set_result(Match.model_validate(msg))
            return
        if mtype == "cdp_response":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_cdp_response(msg)
            return
        if mtype == "cdp_event":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_cdp_event(msg)
            return
        if mtype == "tab_opened":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_tab_opened(msg)
            return
        if mtype in ("session.ended", "session_end"):
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_session_ended(msg)
            return
        if mtype == "chat.message":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser.chat._on_message(msg.get("payload", msg))
            return
        if mtype == "chat.read":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser.chat._on_read(msg.get("payload", msg))
            return
        if mtype == "chat.send_ack":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser.chat._on_send_ack(msg)
            return
        if mtype == "error":
            session_id = msg.get("session_id")
            if session_id and session_id in self._active_browsers:
                await self._active_browsers[session_id]._on_error(msg)
            else:
                self._handle_error(msg)
            return

    def _handle_error(self, msg: dict[str, Any]) -> None:
        code = msg.get("code", 0)
        server_event_id = msg.get("event_id")
        if code == -1013:
            exc: Exception = RateLimitExceeded(retry_after=float(msg.get("retry_after", 1.0)))
        elif code == -1012:
            exc = InsufficientFunds()
        elif code in (-1011, -1018):
            exc = SessionEnded(reason=msg.get("message", "ended"))
        elif code == -1050:
            exc = CdpUnrecoverable(last_error=msg.get("message", "cdp_error"))
        else:
            exc = Exception(f"relay error {code}: {msg.get('message')}")

        if server_event_id:
            fut = self._pending_rents.pop(str(server_event_id), None)
            if fut and not fut.done():
                fut.set_exception(exc)
                return

        # Early error before rent_pending (e.g. -1014 rent failed, -1013 rate limit)
        if self._pending_rent_queue:
            fut = self._pending_rent_queue.pop(0)
            if not fut.done():
                fut.set_exception(exc)
                return

        log.error("unhandled relay error: %s", msg)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending_rent_queue):
            if not fut.done():
                fut.set_exception(exc)
        self._pending_rent_queue.clear()
        for fut in list(self._pending_rents.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending_rents.clear()

    async def _reconnect_loop(self) -> None:
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            delay = BACKOFF_STEPS[min(attempt, len(BACKOFF_STEPS) - 1)]
            log.info("reconnect attempt %d in %ds", attempt + 1, delay)
            await asyncio.sleep(delay)
            try:
                self._ws = await websockets.connect(
                    self.relay_url,
                    subprotocols=[f"bearer.{self.api_key}"],  # type: ignore[arg-type,list-item]
                    extra_headers=self._ws_extra_headers(),
                    open_timeout=20,
                )
                self._last_pong = time.monotonic()
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name="heartbeat"
                )
                self._reader_task = asyncio.create_task(self._reader_loop(), name="reader")
                log.info("reconnected successfully")
                return
            except Exception as exc:
                log.warning("reconnect attempt %d failed: %s", attempt + 1, exc)

        log.error("max reconnect attempts reached")
        self._fail_pending(ConnectionLost("max reconnect attempts reached"))

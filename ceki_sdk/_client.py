from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import websockets
import websockets.exceptions

from ._browser import Browser
from ._exceptions import (
    AuthFailed,
    CdpUnrecoverable,
    CekiError,
    ConnectionLost,
    InsufficientFunds,
    NotOwner,
    RateLimitExceeded,
    SessionEnded,
    SessionExpired,
    SessionNotFound,
)
from ._models import BrowserOption, Match
from ._webrtc import WebRTCTransport

if TYPE_CHECKING:
    from ._models import SessionInfo

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
        chat_url: str,
        reconnect: bool = True,
        basic_auth: tuple[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.relay_url = relay_url
        self.api_url = api_url
        self.chat_url = chat_url
        self.reconnect = reconnect
        self._basic_auth = basic_auth
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending_rents: dict[str, asyncio.Future[Match]] = {}
        self._pending_rent_queue: list[asyncio.Future[Match]] = []
        self._pending_resumes: dict[str, asyncio.Future[dict]] = {}
        self._active_browsers: dict[str, Browser] = {}
        self._backoff_attempt = 0
        self._last_pong = 0.0
        self._closed = False
        self._stashed_first_frame: str | None = None

        # P2P WebRTC transport (primary, WS = fallback)
        self._p2p: WebRTCTransport | None = None
        self._p2p_init_lock = asyncio.Lock()
        self._p2p_enabled: bool = (
            os.environ.get("CEKI_FORCE_WS", "").lower() not in ("1", "true", "yes")
        )
        # ICE servers discovered from webrtc.answer (set by relay)
        self._p2p_ice_servers: list[dict[str, Any]] | None = None

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
            if exc.status_code == 429:
                retry_after = 0
                try:
                    retry_after = int(exc.response_headers.get('Retry-After', 0))
                except (AttributeError, ValueError, TypeError):
                    pass
                raise RateLimitExceeded(retry_after) from exc
            raise
        # Probe for immediate close (4401/4403 post-handshake auth rejection)
        try:
            first = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
            self._stashed_first_frame = first if isinstance(first, str) else first.decode()
        except asyncio.TimeoutError:
            self._stashed_first_frame = None
        except websockets.exceptions.ConnectionClosedError as exc:
            if exc.rcvd and exc.rcvd.code in (4401, 4403):
                reason = exc.rcvd.reason or 'auth_failed'
                raise AuthFailed(
                    f"ws closed with code {exc.rcvd.code}: {reason}"
                ) from exc
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

    async def list_sessions(
        self, *, active: bool = True, limit: int = 50,
    ) -> list["SessionInfo"]:
        from ._models import SessionInfo
        url = f"{self.api_url}/api/agent/sessions"
        headers: dict[str, str] = {"Authorization": f"Bearer {self.api_key}"}
        if self._basic_auth:
            import base64
            raw = f"{self._basic_auth[0]}:{self._basic_auth[1]}"
            creds = base64.b64encode(raw.encode()).decode()
            headers["X-Basic-Auth"] = f"Basic {creds}"
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                url, headers=headers,
                params={"active": "1" if active else "0", "limit": limit},
            )
            resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        return [SessionInfo.model_validate(x) for x in items]

    async def my_browsers(self) -> list[BrowserOption]:
        url = f"{self.api_url}/api/agent/browsers"
        headers: dict[str, str] = {"Authorization": f"Bearer {self.api_key}"}
        if self._basic_auth:
            import base64
            raw = f"{self._basic_auth[0]}:{self._basic_auth[1]}"
            creds = base64.b64encode(raw.encode()).decode()
            headers["X-Basic-Auth"] = f"Basic {creds}"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, headers=headers)
            resp.raise_for_status()
        data = resp.json()
        items = data.get("browsers", data.get("data", data)) if isinstance(data, dict) else data
        return [BrowserOption.model_validate(x) for x in items]

    async def rent(
        self,
        schedule_id: int,
        *,
        mode: str = "incognito",
        human="natural",
        masking_mode: bool = True,
        fingerprint: bool | dict | None = True,
        pacing_profile: str | None = None,
    ) -> Browser:
        if mode not in ("incognito", "main"):
            raise ValueError(f"mode must be 'incognito' or 'main', got {mode!r}")
        fut: asyncio.Future[Match] = asyncio.get_event_loop().create_future()
        self._pending_rent_queue.append(fut)
        msg: dict = {"type": "rent", "browser_id": schedule_id}
        if mode != "incognito":
            msg["mode"] = mode
        if pacing_profile is not None:
            msg["pacing_profile"] = pacing_profile
        await self._ws_send(msg)
        try:
            match = await asyncio.wait_for(fut, timeout=90)
        except asyncio.TimeoutError:
            try:
                self._pending_rent_queue.remove(fut)
            except ValueError:
                pass
            raise TimeoutError("rent timed out waiting for match")
        browser = Browser(client=self, match=match, human=human)
        self._active_browsers[match.session_id] = browser
        if not masking_mode:
            await browser.configure(masking_mode=False)
        if isinstance(fingerprint, dict):
            await browser.configure(fingerprint=fingerprint)
        elif fingerprint is False or fingerprint is None:
            await browser.configure(fingerprint=False)
        return browser

    async def resume(self, session_id: str, *, human="natural") -> Browser:
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending_resumes[session_id] = fut
        await self._ws_send({"type": "resume", "session_id": session_id})
        try:
            resp = await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending_resumes.pop(session_id, None)
            raise TimeoutError("resume timed out")
        match = Match.model_validate(resp)
        browser = Browser(client=self, match=match, human=human)
        self._active_browsers[match.session_id] = browser
        return browser

    async def close(self) -> None:
        self._closed = True
        # Close P2P transport first, then browsers
        if self._p2p is not None:
            await self._p2p.close()
            self._p2p = None
        for browser in list(self._active_browsers.values()):
            await browser.close()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None

    async def disconnect(self) -> None:
        """Close the WS without ending active sessions (for resume-pattern)."""
        self._closed = True
        # Close P2P transport (disconnect WS but keep session)
        if self._p2p is not None:
            await self._p2p.close()
            self._p2p = None
        self._active_browsers.clear()
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
        if self._stashed_first_frame is not None:
            try:
                msg: dict[str, Any] = json.loads(self._stashed_first_frame)
                self._stashed_first_frame = None
                await self._dispatch(msg)
            except Exception as exc:
                log.error("error dispatching stashed frame: %s", exc)
        while not self._closed:
            try:
                assert self._ws is not None
                raw = await self._ws.recv()
                msg = json.loads(raw)
                await self._dispatch(msg)
            except websockets.exceptions.ConnectionClosedError as exc:
                if exc.rcvd and exc.rcvd.code in (4401, 4403):
                    # Server rejected auth post-handshake
                    self._closed = True
                    reason = exc.rcvd.reason
                    err = AuthFailed(
                        f"ws closed with code {exc.rcvd.code}: {reason}"
                    )
                    for fut in list(self._pending_rent_queue):
                        if not fut.done():
                            fut.set_exception(err)
                    self._pending_rent_queue.clear()
                    return
                if not self._closed and self.reconnect:
                    asyncio.create_task(self._reconnect_loop())
                else:
                    self._fail_pending(ConnectionLost("connection closed"))
                break
            except (
                websockets.exceptions.ConnectionClosed,
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
        if mtype == "rent.error":
            code = msg.get("code", "")
            message = msg.get("message", "rent failed")
            eid = msg.get("event_id")
            server_event_id = str(eid) if eid is not None else None
            from ._exceptions import ProviderOffline
            if code == "provider_offline":
                exc_to_raise: Exception = ProviderOffline(message)
            else:
                exc_to_raise = CekiError(message)
            fut: asyncio.Future[Match] | None = None
            if server_event_id:
                fut = self._pending_rents.pop(server_event_id, None)
            if fut is None and self._pending_rent_queue:
                fut = self._pending_rent_queue.pop(0)
            if fut and not fut.done():
                fut.set_exception(exc_to_raise)
            return
        if mtype == "match":
            if msg.get("requires_ack"):
                session_id = msg.get("session_id", "")
                try:
                    await self._ws_send({"type": "match_ack", "session_id": session_id})
                except Exception:
                    pass
            server_event_id = str(msg.get("event_id", ""))
            fut = self._pending_rents.pop(server_event_id, None)
            if fut and not fut.done():
                fut.set_result(Match.model_validate(msg))

            # Initiate P2P WebRTC after successful match
            session_id = msg.get("session_id", "")
            if self._p2p_enabled and session_id:
                asyncio.create_task(
                    self._init_p2p(session_id),
                    name=f"p2p_init_{session_id[:8]}",
                )
            return
        if mtype == "webrtc.answer":
            session_id = msg.get("session_id", "")
            ice_servers = msg.get("ice_servers")
            if ice_servers:
                self._p2p_ice_servers = ice_servers
                # Push to transport for potential future use (re-init)
                if self._p2p is not None:
                    self._p2p.set_ice_servers(ice_servers)
            if self._p2p is not None:
                sdp = msg.get("sdp", "")
                if sdp:
                    asyncio.create_task(
                        self._p2p.set_remote_description(sdp, type="answer"),
                        name=f"p2p_answer_{session_id[:8]}",
                    )
            return
        if mtype == "webrtc.ice_candidate":
            if self._p2p is not None:
                asyncio.create_task(
                    self._p2p.add_ice_candidate(msg),
                    name="p2p_ice_candidate",
                )
            return
        if mtype == "resume_ok":
            sid = msg.get("session_id", "")
            fut = self._pending_resumes.pop(sid, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if mtype == "resume_failed":
            sid = msg.get("session_id", "")
            reason = msg.get("reason", "unknown")
            fut = self._pending_resumes.pop(sid, None)
            exc: Exception
            if reason == "not_owner":
                exc = NotOwner(f"session {sid}: not owner")
            elif reason == "expired":
                exc = SessionExpired(f"session {sid}: expired")
            else:
                exc = SessionNotFound(f"session {sid}: {reason}")
            if fut and not fut.done():
                fut.set_exception(exc)
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
        if mtype == "session.provider_disconnected":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_provider_disconnected(msg)
            return
        if mtype == "session.provider_reconnected":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_provider_reconnected(msg)
            return
        if mtype == "user_events":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                await browser._on_user_events(msg)
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
        if mtype == "chat.error":
            session_id = msg.get("session_id", "")
            browser = self._active_browsers.get(session_id)
            if browser:
                asyncio.create_task(browser.chat._on_send_error(msg))
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
        msg_text = msg.get("reason") or msg.get("message")
        if code == -1013:
            exc: Exception = RateLimitExceeded(retry_after=float(msg.get("retry_after", 1.0)))
        elif code == -1012:
            exc = InsufficientFunds()
        elif code in (-1011, -1018):
            exc = SessionEnded(reason=msg_text or "ended")
        elif code == -1015:
            from ._exceptions import ProviderOffline
            exc = ProviderOffline(msg_text or "no_providers")
        elif code == -1050:
            exc = CdpUnrecoverable(last_error=msg_text or "cdp_error")
        else:
            exc = CekiError(f"relay error {code}: {msg_text}")

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

    # ──────────────────────────────────────────────────────────────────────────
    # P2P WebRTC transport
    # ──────────────────────────────────────────────────────────────────────────

    async def _init_p2p(self, session_id: str) -> None:
        """Initialize P2P WebRTC transport after a successful match.

        Creates a ``WebRTCTransport``, wires ICE candidate callback to send
        via WS signaling, creates an offer, and sends ``webrtc.offer``.

        Mirrors the front flow in ``useWebRTCP2P.js createAndSendOffer``.
        """
        async with self._p2p_init_lock:
            if self._p2p is not None:
                return  # already initialized

            # Merge discovered ICE servers with constructor defaults/environment
            ice_servers = self._p2p_ice_servers or [
                {"urls": "stun:stun.l.google.com:19302"}
            ]

            transport = WebRTCTransport(
                ice_servers=ice_servers,
                ice_transport_policy=os.environ.get("CEKI_ICE_TRANSPORT_POLICY"),
            )

            # Wire ICE candidate callback → WS signaling
            async def _on_ice(candidate: dict[str, Any]) -> None:
                payload = {
                    "type": "webrtc.ice_candidate",
                    "session_id": session_id,
                    "candidate": candidate.get("candidate", ""),
                    "sdp_mid": candidate.get("sdp_mid"),
                    "sdp_mline_index": candidate.get("sdp_mline_index", 0),
                    "fingerprint": transport.extract_fingerprint() or "",
                }
                try:
                    await self._ws_send(payload)
                except Exception as exc:
                    log.warning("p2p: failed to send ICE candidate: %s", exc)

            transport.on_ice_candidate = _on_ice

            # Wire CDP message callback → route to active browser
            async def _on_cdp(msg: dict[str, Any]) -> None:
                cmd_id = msg.get("id")
                method = msg.get("method", "")
                session_id_dc = msg.get("session_id", session_id)
                browser = self._active_browsers.get(session_id_dc)
                if browser:
                    if cmd_id is not None:
                        await browser._on_cdp_response(msg)
                    elif method:
                        await browser._on_cdp_event(msg)

            transport.on_cdp_message = _on_cdp

            self._p2p = transport

        try:
            offer_sdp = await transport.create_offer()
            fingerprint = transport.extract_fingerprint() or ""

            log.info(
                "p2p: sending webrtc.offer session_id=%s sdp_len=%d fingerprint=%s",
                session_id[:8],
                len(offer_sdp),
                fingerprint[:16] if fingerprint else "none",
            )

            await self._ws_send({
                "type": "webrtc.offer",
                "session_id": session_id,
                "sdp": offer_sdp,
                "fingerprint": fingerprint,
            })
        except Exception as exc:
            log.error("p2p: failed to create/send offer: %s", exc)
            # Fallback: P2P failed, WS path continues to work
            if self._p2p is not None:
                await self._p2p.close()
                self._p2p = None

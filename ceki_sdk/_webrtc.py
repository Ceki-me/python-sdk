"""WebRTC transport for P2P CDP communication.

Wraps ``aiortc.RTCPeerConnection`` to provide a WebRTC DataChannel-based
transport for CDP commands. Used as primary transport for agent-renters,
with WebSocket as fallback.

Protocol (mirrors front useWebRTCP2P.js):
1. After ``match`` → create RTCPeerConnection + DataChannel('ceki-cmd')
2. createOffer → setLocalDescription → extract DTLS fingerprint → send
   ``webrtc.offer {session_id, sdp, fingerprint}`` via WS signaling
3. Receive ``webrtc.answer`` → setRemoteDescription → ICE exchange
4. ICE candidates: local → ``webrtc.ice_candidate`` via WS;
   remote → addIceCandidate
5. ``ceki-cmd`` DC open → CDP JSON commands sent over DC instead of WS
6. Inbound CDP responses/events arrive on DC → forwarded to callback
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)

# SDP fingerprint extraction regex (mirrors front extractFingerprint)
_FINGERPRINT_RE = re.compile(r"a=fingerprint:(sha-\d+) (\S+)", re.IGNORECASE)


def _parse_ice_candidate(
    raw: str,
    sdp_mid: str | None = None,
    sdp_mline_index: int = 0,
) -> Any:
    """Parse an SDP-format ICE candidate string into an ``RTCIceCandidate``.

    The SDP candidate format is::

        candidate:FOUNDATION COMPONENT PROTOCOL PRIORITY IP PORT ...

    Optional trailing attributes (``typ host generation 0``, etc.) are
    parsed if present. Returns ``None`` if parsing fails.
    """
    try:
        from aiortc import RTCIceCandidate
    except ImportError:
        raise ImportError("aiortc is required for P2P WebRTC transport")

    text = raw.replace("candidate:", "", 1) if raw.startswith("candidate:") else raw
    parts = text.split()

    if len(parts) < 6:
        log.warning("webrtc: malformed ICE candidate: %s", raw)
        # Return a minimal placeholder so the caller doesn't crash
        return RTCIceCandidate(
            component=1,
            foundation="0",
            ip="0.0.0.0",
            port=9,
            priority=0,
            protocol="UDP",
            type="host",
            sdpMid=sdp_mid,
            sdpMLineIndex=sdp_mline_index,
        )

    foundation = parts[0]
    component = int(parts[1])
    protocol = parts[2]
    priority = int(parts[3])
    ip = parts[4]
    port = int(parts[5])

    # Parse optional type
    cand_type = "host"
    for i, part in enumerate(parts):
        if part == "typ" and i + 1 < len(parts):
            cand_type = parts[i + 1]
            break

    return RTCIceCandidate(
        component=component,
        foundation=foundation,
        ip=ip,
        port=port,
        priority=priority,
        protocol=protocol,
        type=cand_type,
        sdpMid=sdp_mid,
        sdpMLineIndex=sdp_mline_index,
    )


class WebRTCTransport:
    """WebRTC peer connection wrapper for P2P CDP transport.

    This is the python-sdk counterpart of the browser extension's
    ``RtcBridge`` / ``RtcPeer`` classes. It manages one
    ``aiortc.RTCPeerConnection`` with a single ``ceki-cmd`` data channel
    for CDP command/response exchange.

    Usage::

        transport = WebRTCTransport(ice_servers=[...])
        transport.on_ice_candidate = lambda cand: ws_send(...)
        transport.on_cdp_message = lambda msg: handle_cdp(msg)

        offer_sdp = await transport.create_offer()
        fingerprint = transport.extract_fingerprint()
        # send webrtc.offer {session_id, sdp, fingerprint} via WS

        # on webrtc.answer:
        await transport.set_remote_description(answer_sdp)

        # on webrtc.ice_candidate:
        await transport.add_ice_candidate(candidate)
    """

    def __init__(
        self,
        ice_servers: list[dict[str, Any]] | None = None,
        ice_transport_policy: str | None = None,
    ) -> None:
        self._pc: Any = None  # aiortc.RTCPeerConnection
        self._cmd_dc: Any = None  # aiortc.RTCDataChannel

        # ICE servers: constructor arg → CEKI_TURN_SERVERS env → default STUN
        env_servers_raw = os.environ.get("CEKI_TURN_SERVERS")
        env_servers: list[dict[str, Any]] = []
        if env_servers_raw:
            try:
                parsed = json.loads(env_servers_raw)
                if isinstance(parsed, list):
                    env_servers = parsed
                else:
                    log.warning("webrtc: CEKI_TURN_SERVERS is not a JSON array, ignoring")
            except json.JSONDecodeError:
                log.warning("webrtc: CEKI_TURN_SERVERS is not valid JSON, ignoring")
        merged = list(ice_servers) if ice_servers else []
        seen_urls: set[str] = set()
        for srv in merged:
            urls = srv.get("urls", "")
            if isinstance(urls, str):
                seen_urls.add(urls)
            elif isinstance(urls, list):
                seen_urls.update(urls)
        for srv in env_servers:
            urls = srv.get("urls", "")
            if isinstance(urls, str):
                if urls not in seen_urls:
                    merged.append(srv)
                    if isinstance(urls, str):
                        seen_urls.add(urls)
            elif isinstance(urls, list):
                new_urls = [u for u in urls if u not in seen_urls]
                if new_urls:
                    srv = {**srv, "urls": new_urls}
                    merged.append(srv)
                    seen_urls.update(new_urls)

        self._ice_servers = merged or [{"urls": "stun:stun.l.google.com:19302"}]

        # ICE transport policy: constructor arg → CEKI_ICE_TRANSPORT_POLICY env → "all"
        self._ice_transport_policy = (
            ice_transport_policy
            or os.environ.get("CEKI_ICE_TRANSPORT_POLICY", "all")
        )
        self._local_fingerprint: str | None = None
        self._closed = False
        self._pending_remote_candidates: list[Any] = []

        # Callbacks — set by consumer (_client.py)
        self.on_ice_candidate: Callable[[dict[str, Any]], Coroutine[Any, Any, None] | None] | None = None
        self.on_cdp_message: Callable[[dict[str, Any]], Coroutine[Any, Any, None] | None] | None = None
        self.on_connection_state: Callable[[str], Coroutine[Any, Any, None] | None] | None = None
        self.on_data_channel_state: Callable[[str], Coroutine[Any, Any, None] | None] | None = None

        # DataChannel open event — used by _browser.send() to wait for DC
        # readiness before sending CDP (prevents startup-race WS congestion).
        self._dc_open_event = asyncio.Event()

    async def _ensure_pc(self) -> Any:
        """Lazy-create the RTCPeerConnection on first use."""
        if self._pc is not None:
            return self._pc

        try:
            from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
        except ImportError:
            raise ImportError(
                "aiortc is required for P2P WebRTC transport. "
                "Install it: pip install aiortc"
            )

        config = RTCConfiguration(
            iceServers=[
                RTCIceServer(**srv) if isinstance(srv, dict) else srv
                for srv in self._ice_servers
            ]
        )
        if self._ice_transport_policy == "relay":
            config.iceTransportPolicy = "relay"

        self._pc = RTCPeerConnection(configuration=config)

        # Wire ICE candidate callback
        @self._pc.on("icecandidate")
        async def _on_ice(candidate: Any) -> None:
            if candidate is None:
                # ICE gathering complete
                return
            if self.on_ice_candidate:
                await self.on_ice_candidate({
                    "candidate": candidate.candidate,
                    "sdp_mid": candidate.sdpMid,
                    "sdp_mline_index": candidate.sdpMLineIndex,
                })

        # Wire connection state
        @self._pc.on("connectionstatechange")
        async def _on_conn_state() -> None:
            state = self._pc.connectionState if self._pc else "closed"
            if self.on_connection_state:
                await self.on_connection_state(state)

        # Handle incoming data channels (host-side — provider creates capture DC)
        @self._pc.on("datachannel")
        def _on_dc(channel: Any) -> None:
            log.info("webrtc: incoming data channel: %s", channel.label)
            if channel.label == "ceki-cmd":
                self._cmd_dc = channel
                self._wire_cmd_dc(channel)
            elif channel.label == "ceki-capture":
                # Agent doesn't process capture frames, but log it
                log.info("webrtc: ceki-capture channel opened (no-op for agent)")

        return self._pc

    def _wire_cmd_dc(self, channel: Any) -> None:
        """Set up message/close handlers on the ceki-cmd data channel."""

        @channel.on("open")
        async def _on_open() -> None:
            log.info("webrtc: ceki-cmd DC opened")
            self._dc_open_event.set()
            if self.on_data_channel_state:
                await self.on_data_channel_state("open")

        @channel.on("close")
        async def _on_close() -> None:
            log.info("webrtc: ceki-cmd DC closed")
            if self.on_data_channel_state:
                await self.on_data_channel_state("closed")

        @channel.on("message")
        async def _on_message(message: str | bytes) -> None:
            try:
                data = json.loads(message if isinstance(message, str) else message.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.warning("webrtc: failed to parse DC message: %s", exc)
                return

            if self.on_cdp_message:
                await self.on_cdp_message(data)

    async def create_offer(self) -> str:
        """Create and set local offer, return the SDP string.

        Also creates the ``ceki-cmd`` data channel before generating the offer
        so the SDP includes it (mirrors front setupCmdChannel).
        """
        pc = await self._ensure_pc()

        # Reset DC-open event for the new connection
        self._dc_open_event.clear()

        # Create ceki-cmd data channel (renter→host CDP commands)
        self._cmd_dc = pc.createDataChannel("ceki-cmd", ordered=True)
        self._wire_cmd_dc(self._cmd_dc)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        self._cache_fingerprint(pc.localDescription.sdp)
        return pc.localDescription.sdp

    async def create_answer(self, remote_sdp: str) -> str:
        """Set remote offer, create and set local answer, return answer SDP."""
        try:
            from aiortc import RTCSessionDescription
        except ImportError:
            raise ImportError("aiortc is required for P2P WebRTC transport")

        pc = await self._ensure_pc()
        remote_desc = RTCSessionDescription(sdp=remote_sdp, type="offer")
        await pc.setRemoteDescription(remote_desc)

        # Flush pending ICE candidates (queued before remote was set)
        pending = self._pending_remote_candidates
        self._pending_remote_candidates = []
        for cand in pending:
            try:
                await pc.addIceCandidate(cand)
            except Exception as exc:
                log.warning("webrtc: failed to add queued ICE candidate: %s", exc)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        self._cache_fingerprint(pc.localDescription.sdp)
        return pc.localDescription.sdp

    async def set_remote_description(self, sdp: str, type: str = "answer") -> None:
        """Set remote description (answer from host)."""
        try:
            from aiortc import RTCSessionDescription
        except ImportError:
            raise ImportError("aiortc is required for P2P WebRTC transport")

        pc = await self._ensure_pc()
        remote_desc = RTCSessionDescription(sdp=sdp, type=type)
        await pc.setRemoteDescription(remote_desc)

        # Flush pending ICE candidates
        pending = self._pending_remote_candidates
        self._pending_remote_candidates = []
        for cand in pending:
            try:
                await pc.addIceCandidate(cand)
            except Exception as exc:
                log.warning("webrtc: failed to add queued ICE candidate: %s", exc)

    async def add_ice_candidate(self, candidate: dict[str, Any]) -> None:
        """Add a remote ICE candidate.

        Queues the candidate if remote description hasn't been set yet
        (mirrors front pendingCandidates pattern).
        """
        try:
            from aiortc import RTCIceCandidate
        except ImportError:
            raise ImportError("aiortc is required for P2P WebRTC transport")

        raw_candidate = candidate.get("candidate", "")
        cand = _parse_ice_candidate(
            raw_candidate,
            sdp_mid=candidate.get("sdp_mid"),
            sdp_mline_index=candidate.get("sdp_mline_index", 0),
        )

        pc = self._pc
        if pc is None or pc.remoteDescription is None:
            self._pending_remote_candidates.append(cand)
            return

        try:
            await pc.addIceCandidate(cand)
        except Exception as exc:
            log.warning("webrtc: failed to add ICE candidate: %s", exc)

    def extract_fingerprint(self) -> str | None:
        """Return cached DTLS fingerprint from local SDP.

        The fingerprint is extracted from the local SDP after
        ``setLocalDescription`` and cached. It is sent as part of
        the ``webrtc.offer`` / ``webrtc.ice_candidate`` signaling
        messages (mirrors front extractFingerprint).
        """
        return self._local_fingerprint

    def set_ice_servers(self, ice_servers: list[dict[str, Any]]) -> None:
        """Update the ICE server list for future use.

        If the RTCPeerConnection has not been created yet (``_ensure_pc``
        not called), the new servers will be used when it is first created.
        If the PC already exists, the servers are stored for potential
        future reconnection.

        De-duplicates by ``urls`` field against existing servers.
        """
        seen_urls: set[str] = set()
        for srv in self._ice_servers:
            urls = srv.get("urls", "")
            if isinstance(urls, str):
                seen_urls.add(urls)
            elif isinstance(urls, list):
                seen_urls.update(urls)
        for srv in ice_servers:
            urls = srv.get("urls", "")
            if isinstance(urls, str):
                if urls not in seen_urls:
                    self._ice_servers.append(srv)
                    seen_urls.add(urls)
            elif isinstance(urls, list):
                new_urls = [u for u in urls if u not in seen_urls]
                if new_urls:
                    self._ice_servers.append({**srv, "urls": new_urls})
                    seen_urls.update(new_urls)

    def set_ice_transport_policy(self, policy: str) -> None:
        """Set ICE transport policy for future use (``"all"`` or ``"relay"``).

        Like ``set_ice_servers``, only applies to a new PC if ``_ensure_pc``
        has not been called yet.
        """
        if policy not in ("all", "relay"):
            raise ValueError(f"ICE transport policy must be 'all' or 'relay', got {policy!r}")
        self._ice_transport_policy = policy

    def _cache_fingerprint(self, sdp: str) -> None:
        """Extract and cache DTLS fingerprint from SDP."""
        match = _FINGERPRINT_RE.search(sdp)
        if match:
            self._local_fingerprint = match.group(2)
            log.debug("webrtc: cached fingerprint %s:%s", match.group(1), match.group(2))
        else:
            self._local_fingerprint = None
            log.warning("webrtc: no fingerprint found in SDP")

    async def send_cdp(self, msg: dict[str, Any]) -> None:
        """Send a CDP message over the ceki-cmd data channel.

        Raises ``ConnectionError`` if the data channel is not open.
        """
        if self._cmd_dc is None or self._cmd_dc.readyState != "open":
            raise ConnectionError("ceki-cmd DC not open")
        self._cmd_dc.send(json.dumps(msg))

    @property
    def is_connected(self) -> bool:
        """Whether the P2P connection is established."""
        if self._pc is None:
            return False
        return self._pc.connectionState == "connected"

    @property
    def cmd_dc_open(self) -> bool:
        """Whether the ceki-cmd data channel is open."""
        if self._cmd_dc is None:
            return False
        return self._cmd_dc.readyState == "open"

    async def wait_dc_open(self) -> None:
        """Wait for the ceki-cmd data channel to open.

        Used by ``Browser.send()`` to prevent CDP from being sent over WS
        before P2P DC is ready (startup-race guard). The caller wraps this
        with ``asyncio.wait_for`` for timeout handling.
        """
        await self._dc_open_event.wait()

    async def close(self) -> None:
        """Close the peer connection and cleanup."""
        self._closed = True
        if self._cmd_dc is not None:
            try:
                self._cmd_dc.close()
            except Exception:
                pass
            self._cmd_dc = None
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None
        self._dc_open_event.clear()
        self._local_fingerprint = None
        self._pending_remote_candidates.clear()
        log.info("webrtc: transport closed")

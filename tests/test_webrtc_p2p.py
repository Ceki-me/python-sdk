"""Unit tests for P2P WebRTC transport and WS fallback.

Tests cover:
- WebRTCTransport construction, ICE server dedup, env var integration
- SDP fingerprint extraction
- Data channel state properties
- send_cdp() when DC not open → ConnectionError
- ICE candidate queuing (before remote description)
- set_ice_servers / set_ice_transport_policy
- Close / cleanup
- CEKI_FORCE_WS, CEKI_TURN_SERVERS, CEKI_ICE_TRANSPORT_POLICY env vars
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

_SAMPLE_SDP_WITH_FINGERPRINT = (
    "v=0\r\n"
    "o=- 12345 2 IN IP4 0.0.0.0\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0\r\n"
    "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
)

_SAMPLE_SDP_NO_FINGERPRINT = (
    "v=0\r\n"
    "o=- 12345 2 IN IP4 0.0.0.0\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
)


def _make_ice_candidate_dict(
    candidate: str = "candidate:1 1 UDP 12345 1.2.3.4 1234 typ host",
    sdp_mid: str = "0",
    sdp_mline_index: int = 0,
) -> dict[str, Any]:
    return {
        "candidate": candidate,
        "sdp_mid": sdp_mid,
        "sdp_mline_index": sdp_mline_index,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tests — WebRTCTransport construction & env
# ──────────────────────────────────────────────────────────────────────────────


def test_constructor_defaults():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    assert t._ice_transport_policy == "all"
    assert t._pc is None
    assert t._cmd_dc is None
    assert t._local_fingerprint is None
    assert not t._closed
    assert t._pending_remote_candidates == []
    # Default STUN server
    assert len(t._ice_servers) == 1
    assert t._ice_servers[0]["urls"] == "stun:stun.l.google.com:19302"


def test_constructor_with_ice_servers():
    from ceki_sdk._webrtc import WebRTCTransport

    servers = [{"urls": "stun:stun.example.com:3478"}]
    t = WebRTCTransport(ice_servers=servers)
    assert t._ice_servers == servers


def test_constructor_ice_transport_policy_override():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport(ice_transport_policy="relay")
    assert t._ice_transport_policy == "relay"


def test_constructor_env_turn_servers(monkeypatch):
    from ceki_sdk._webrtc import WebRTCTransport

    turn_json = json.dumps([
        {"urls": "turn:turn.example.com:3478", "username": "user", "credential": "pass"},
    ])
    monkeypatch.setenv("CEKI_TURN_SERVERS", turn_json)
    t = WebRTCTransport()
    assert any("turn:turn.example.com:3478" in str(srv) for srv in t._ice_servers)


def test_constructor_env_turn_servers_invalid_json(monkeypatch):
    from ceki_sdk._webrtc import WebRTCTransport

    monkeypatch.setenv("CEKI_TURN_SERVERS", "not-json")
    t = WebRTCTransport()
    # Should gracefully fall back to default STUN
    assert len(t._ice_servers) == 1
    assert t._ice_servers[0]["urls"] == "stun:stun.l.google.com:19302"


def test_constructor_env_ice_transport_policy(monkeypatch):
    from ceki_sdk._webrtc import WebRTCTransport

    monkeypatch.setenv("CEKI_ICE_TRANSPORT_POLICY", "relay")
    t = WebRTCTransport()
    assert t._ice_transport_policy == "relay"


def test_constructor_env_trumps_default(monkeypatch):
    from ceki_sdk._webrtc import WebRTCTransport

    # Explicit arg should win over env
    monkeypatch.setenv("CEKI_ICE_TRANSPORT_POLICY", "relay")
    t = WebRTCTransport(ice_transport_policy="all")
    assert t._ice_transport_policy == "all"


def test_constructor_env_dedup_with_arg(monkeypatch):
    from ceki_sdk._webrtc import WebRTCTransport

    monkeypatch.setenv(
        "CEKI_TURN_SERVERS",
        json.dumps([{"urls": "stun:stun.l.google.com:19302"}]),
    )
    t = WebRTCTransport()
    # Same STUN server from env — should not duplicate
    assert len(t._ice_servers) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Tests — Fingerprint extraction
# ──────────────────────────────────────────────────────────────────────────────


def test_extract_fingerprint():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    assert t.extract_fingerprint() is None  # not cached yet
    t._cache_fingerprint(_SAMPLE_SDP_WITH_FINGERPRINT)
    assert t.extract_fingerprint() == (
        "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
        "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
    )


def test_extract_fingerprint_no_match():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    t._cache_fingerprint(_SAMPLE_SDP_NO_FINGERPRINT)
    assert t.extract_fingerprint() is None


# ──────────────────────────────────────────────────────────────────────────────
# Tests — Data channel state
# ──────────────────────────────────────────────────────────────────────────────


def test_cmd_dc_open_no_dc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    assert not t.cmd_dc_open


def test_cmd_dc_open_with_non_open_dc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_dc = MagicMock()
    mock_dc.readyState = "connecting"
    t._cmd_dc = mock_dc
    assert not t.cmd_dc_open


def test_cmd_dc_open_true():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_dc = MagicMock()
    mock_dc.readyState = "open"
    t._cmd_dc = mock_dc
    assert t.cmd_dc_open


def test_is_connected_no_pc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    assert not t.is_connected


def test_is_connected_pc_connected():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_pc = MagicMock()
    mock_pc.connectionState = "connected"
    t._pc = mock_pc
    assert t.is_connected


def test_is_connected_pc_not_connected():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_pc = MagicMock()
    mock_pc.connectionState = "failed"
    t._pc = mock_pc
    assert not t.is_connected


# ──────────────────────────────────────────────────────────────────────────────
# Tests — send_cdp
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_cdp_no_dc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    with pytest.raises(ConnectionError, match="ceki-cmd DC not open"):
        await t.send_cdp({"id": 1, "method": "Page.navigate"})


@pytest.mark.asyncio
async def test_send_cdp_dc_not_open():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_dc = MagicMock()
    mock_dc.readyState = "connecting"
    t._cmd_dc = mock_dc
    with pytest.raises(ConnectionError, match="ceki-cmd DC not open"):
        await t.send_cdp({"id": 1, "method": "Page.navigate"})


@pytest.mark.asyncio
async def test_send_cdp_ok():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_dc = MagicMock()
    mock_dc.readyState = "open"
    t._cmd_dc = mock_dc

    msg = {"id": 42, "method": "Page.navigate", "params": {"url": "https://example.com"}}
    await t.send_cdp(msg)
    expected_json = json.dumps(msg)
    mock_dc.send.assert_called_once_with(expected_json)


# ──────────────────────────────────────────────────────────────────────────────
# Tests — ICE candidate queuing
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_ice_candidate_no_pc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    cand = _make_ice_candidate_dict()
    await t.add_ice_candidate(cand)
    assert len(t._pending_remote_candidates) == 1


@pytest.mark.asyncio
async def test_add_ice_candidate_pc_no_remote_desc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    t._pc = MagicMock()
    t._pc.remoteDescription = None
    cand = _make_ice_candidate_dict()
    await t.add_ice_candidate(cand)
    assert len(t._pending_remote_candidates) == 1


@pytest.mark.asyncio
async def test_queued_candidates_pending_until_remote_desc():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    cand = _make_ice_candidate_dict()
    await t.add_ice_candidate(cand)
    assert len(t._pending_remote_candidates) == 1

    # With PC + remoteDescription set, candidate goes directly to addIceCandidate
    mock_pc = MagicMock()
    mock_pc.remoteDescription = MagicMock()
    mock_pc.addIceCandidate = AsyncMock()
    t._pc = mock_pc

    # Now add another candidate — should go to PC directly, not queue
    await t.add_ice_candidate(_make_ice_candidate_dict(candidate="candidate:2 1 UDP 54321 5.6.7.8 5678 typ host"))
    assert len(t._pending_remote_candidates) == 1  # still only the first one
    mock_pc.addIceCandidate.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# Tests — set_ice_servers
# ──────────────────────────────────────────────────────────────────────────────


def test_set_ice_servers_dedup():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    initial_len = len(t._ice_servers)

    # Adding same STUN server again should dedup
    t.set_ice_servers([{"urls": "stun:stun.l.google.com:19302"}])
    assert len(t._ice_servers) == initial_len

    # Adding a new TURN server should append
    t.set_ice_servers([{"urls": "turn:turn.example.com:3478", "username": "u", "credential": "p"}])
    assert len(t._ice_servers) == initial_len + 1
    assert any("turn.example.com" in str(srv) for srv in t._ice_servers)

    # Same TURN server again — should dedup
    t.set_ice_servers([{"urls": "turn:turn.example.com:3478"}])
    assert len(t._ice_servers) == initial_len + 1


def test_set_ice_servers_list_urls():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    initial_len = len(t._ice_servers)

    t.set_ice_servers([{"urls": ["turn:a.example.com:3478", "turn:b.example.com:3478"]}])
    assert len(t._ice_servers) == initial_len + 1


def test_set_ice_servers_partial_dedup_list():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    # Add a server with urls as a list where one already exists
    t.set_ice_servers([{
        "urls": ["stun:stun.l.google.com:19302", "turn:unique.example.com:3478"],
    }])
    # The STUN url should be dedup'd, the TURN one added
    found = [s for s in t._ice_servers if "unique.example.com" in str(s)]
    assert len(found) == 1
    entry = found[0]
    # The STUN url should have been filtered out
    assert "stun.l.google.com" not in str(entry.get("urls"))


# ──────────────────────────────────────────────────────────────────────────────
# Tests — set_ice_transport_policy
# ──────────────────────────────────────────────────────────────────────────────


def test_set_ice_transport_policy_valid():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    t.set_ice_transport_policy("relay")
    assert t._ice_transport_policy == "relay"


def test_set_ice_transport_policy_invalid():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    with pytest.raises(ValueError, match="ICE transport policy"):
        t.set_ice_transport_policy("foo")


# ──────────────────────────────────────────────────────────────────────────────
# Tests — close
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_cleanup():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    mock_dc = MagicMock()
    mock_pc = MagicMock()
    # Simulate pc.close being async in aiortc
    mock_pc.close = AsyncMock()
    t._cmd_dc = mock_dc
    t._pc = mock_pc
    t._pending_remote_candidates.append("dummy")

    await t.close()

    assert t._closed
    assert t._cmd_dc is None
    assert t._pc is None
    assert t._local_fingerprint is None
    assert t._pending_remote_candidates == []
    mock_dc.close.assert_called_once()
    mock_pc.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_idempotent():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    await t.close()
    # Second close should not raise
    await t.close()


# ──────────────────────────────────────────────────────────────────────────────
# Tests — CEKI_FORCE_WS flag (Client-level)
# ──────────────────────────────────────────────────────────────────────────────


def test_force_ws_env_disables_p2p(monkeypatch):
    from ceki_sdk._client import Client

    monkeypatch.setenv("CEKI_FORCE_WS", "1")
    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    assert not c._p2p_enabled


def test_force_ws_env_default_enables_p2p():
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    assert c._p2p_enabled


def test_force_ws_false_values(monkeypatch):
    from ceki_sdk._client import Client

    for val in ("0", "false", "no"):
        monkeypatch.setenv("CEKI_FORCE_WS", val)
        c = Client(api_key="test", relay_url="ws://localhost:9999",
                   api_url="https://api.example.com", chat_url="https://chat.example.com")
        assert c._p2p_enabled, f"expected enabled for CEKI_FORCE_WS={val!r}"


# ──────────────────────────────────────────────────────────────────────────────
# Tests — Browser.send() P2P routing (via mock)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_send_falls_back_to_ws_when_no_p2p():
    """Without P2P active, Browser.send() should send via _ws_send."""
    from ceki_sdk._browser import Browser
    from ceki_sdk._models import Match

    client = MagicMock()
    client._p2p = None
    client._ws_send = AsyncMock()

    match = MagicMock(spec=Match)
    match.session_id = "test-session"
    match.schedule_id = 42
    match.browser_info = {}
    match.provider_user_id = 1
    match.event_id = 999
    match.chat_topic_id = None

    browser = Browser(client=client, match=match)
    browser._ended.is_set = MagicMock(return_value=False)

    cdp = {"method": "Page.navigate", "params": {"url": "https://example.com"}}

    # Schedule the send and then cancel it (it will hang on fut)
    task = asyncio.create_task(browser.send(cdp, timeout=999))

    # Give it a moment to call _ws_send
    await asyncio.sleep(0.1)

    # Verify _ws_send was called with CDP message
    client._ws_send.assert_called_once()
    call_args = client._ws_send.call_args[0][0]
    assert call_args["type"] == "cdp"
    assert call_args["session_id"] == "test-session"
    assert call_args["method"] == "Page.navigate"

    # Resolve pending future to avoid warnings
    fut = browser._pending_cdp.get(0)
    if fut and not fut.done():
        fut.cancel()

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_browser_send_p2p_path_when_dc_open():
    """When P2P data channel is open, Browser.send() should send via DC."""
    from ceki_sdk._browser import Browser
    from ceki_sdk._models import Match

    p2p_mock = MagicMock()
    p2p_mock.cmd_dc_open = True
    p2p_mock.send_cdp = AsyncMock()
    # Mock wait_dc_open() to succeed immediately (DC is ready)
    p2p_mock.wait_dc_open = AsyncMock()

    client = MagicMock()
    client._p2p = p2p_mock
    client._ws_send = AsyncMock()

    match = MagicMock(spec=Match)
    match.session_id = "test-session"
    match.schedule_id = 42
    match.browser_info = {}
    match.provider_user_id = 1
    match.event_id = 999
    match.chat_topic_id = None

    browser = Browser(client=client, match=match)
    browser._ended.is_set = MagicMock(return_value=False)

    cdp = {"method": "Page.navigate", "params": {"url": "https://example.com"}}

    task = asyncio.create_task(browser.send(cdp, timeout=999))
    await asyncio.sleep(0.1)

    # Should NOT have called _ws_send
    client._ws_send.assert_not_called()
    # Should have called send_cdp on the transport
    p2p_mock.send_cdp.assert_called_once()
    call_args = p2p_mock.send_cdp.call_args[0][0]
    assert call_args["session_id"] == "test-session"
    assert call_args["method"] == "Page.navigate"

    # Cleanup
    fut = browser._pending_cdp.get(0)
    if fut and not fut.done():
        fut.cancel()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Tests — _client.py dispatch handlers
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_webrtc_answer_sets_remote_description():
    """webrtc.answer dispatch should call set_remote_description on the transport."""
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    p2p_mock = MagicMock()
    p2p_mock.set_remote_description = AsyncMock()
    # Mock wait_dc_open to succeed immediately
    p2p_mock.wait_dc_open = AsyncMock()
    c._p2p = p2p_mock

    answer_msg = {
        "type": "webrtc.answer",
        "session_id": "test",
        "sdp": "v=0\r\n",
    }
    await c._dispatch(answer_msg)

    p2p_mock.set_remote_description.assert_called_once_with("v=0\r\n", type="answer")


@pytest.mark.asyncio
async def test_dispatch_webrtc_answer_no_ice_servers():
    """webrtc.answer without ice_servers should not crash."""
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    p2p_mock = MagicMock()
    p2p_mock.set_remote_description = AsyncMock()
    # Mock wait_dc_open to succeed immediately
    p2p_mock.wait_dc_open = AsyncMock()
    c._p2p = p2p_mock

    answer_msg = {"type": "webrtc.answer", "session_id": "test", "sdp": "v=0\r\n"}
    # Should not raise
    await c._dispatch(answer_msg)

    p2p_mock.set_remote_description.assert_called_once_with("v=0\r\n", type="answer")


@pytest.mark.asyncio
async def test_dispatch_webrtc_answer_no_ice_servers():
    """webrtc.answer without ice_servers should not crash."""
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    p2p_mock = MagicMock()
    p2p_mock.set_remote_description = AsyncMock()
    c._p2p = p2p_mock

    answer_msg = {"type": "webrtc.answer", "session_id": "test", "sdp": "v=0\r\n"}
    # Should not raise
    await c._dispatch(answer_msg)


@pytest.mark.asyncio
async def test_dispatch_webrtc_ice_candidate_no_p2p():
    """webrtc.ice_candidate without P2P transport should not crash."""
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    c._p2p = None  # No P2P yet

    await c._dispatch({"type": "webrtc.ice_candidate", "session_id": "test"})


@pytest.mark.asyncio
async def test_dispatch_webrtc_ice_candidate_with_p2p():
    """webrtc.ice_candidate with P2P should call add_ice_candidate."""
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    p2p_mock = MagicMock()
    p2p_mock.add_ice_candidate = AsyncMock()
    c._p2p = p2p_mock

    await c._dispatch({
        "type": "webrtc.ice_candidate",
        "session_id": "test",
        "candidate": "candidate:1 1 UDP 12345 1.2.3.4 1234 typ host",
    })
    p2p_mock.add_ice_candidate.assert_called_once()


def test_client_p2p_enabled_default():
    """Client should have P2P enabled by default."""
    from ceki_sdk._client import Client

    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    assert c._p2p_enabled


def test_client_p2p_disabled_via_env(monkeypatch):
    """CEKI_FORCE_WS=1 should disable P2P."""
    from ceki_sdk._client import Client

    monkeypatch.setenv("CEKI_FORCE_WS", "1")
    c = Client(api_key="test", relay_url="ws://localhost:9999",
               api_url="https://api.example.com", chat_url="https://chat.example.com")
    assert not c._p2p_enabled


# ──────────────────────────────────────────────────────────────────────────────
# Tests — chunked CDP responses over DC (cdp-response-chunk reassembly)
# ──────────────────────────────────────────────────────────────────────────────


class _RecordingDC:
    """Minimal fake data channel that captures registered event handlers."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str):
        def deco(fn):
            self.handlers[event] = fn
            return fn

        return deco


def _make_transport_with_message_handler():
    from ceki_sdk._webrtc import WebRTCTransport

    t = WebRTCTransport()
    dc = _RecordingDC()
    t._wire_cmd_dc(dc)
    return t, dc


@pytest.mark.asyncio
async def test_dc_small_message_passes_through_unchanged():
    """A non-chunk message must reach on_cdp_message verbatim."""
    t, dc = _make_transport_with_message_handler()
    received: list[dict[str, Any]] = []

    async def capture(msg: dict[str, Any]) -> None:
        received.append(msg)

    t.on_cdp_message = capture

    msg = {"type": "cdp_response", "id": 1, "result": {"ok": True}}
    await dc.handlers["message"](json.dumps(msg))

    assert received == [msg]
    assert t._pending_chunks == {}


@pytest.mark.asyncio
async def test_dc_chunked_message_reassembled():
    """A chunked CDP response reassembles into the original message."""
    t, dc = _make_transport_with_message_handler()
    received: list[dict[str, Any]] = []

    async def capture(msg: dict[str, Any]) -> None:
        received.append(msg)

    t.on_cdp_message = capture

    original = {"type": "cdp_response", "id": 99, "result": {"data": "x" * 120_000}}
    full = json.dumps(original)
    # Mirror the extension's chunking constants
    chunk_size = 48000
    total = (len(full) + chunk_size - 1) // chunk_size
    chunk_id = "chunk-test-1"
    assert total > 1

    for i in range(total):
        chunk = {
            "type": "cdp-response-chunk",
            "chunkId": chunk_id,
            "seq": i,
            "total": total,
            "payload": full[i * chunk_size : (i + 1) * chunk_size],
        }
        if i == 0:
            chunk["meta"] = {"id": 99}
        await dc.handlers["message"](json.dumps(chunk))

    assert len(received) == 1
    assert received[0] == original
    assert t._pending_chunks == {}


@pytest.mark.asyncio
async def test_dc_chunk_reassembly_out_of_order():
    """Chunks may arrive out of order; reassembly still yields the message."""
    t, dc = _make_transport_with_message_handler()
    received: list[dict[str, Any]] = []

    async def capture(msg: dict[str, Any]) -> None:
        received.append(msg)

    t.on_cdp_message = capture

    original = {"type": "cdp_response", "id": 55, "result": {"data": "y" * 120_000}}
    full = json.dumps(original)
    chunk_size = 48000
    total = (len(full) + chunk_size - 1) // chunk_size
    chunks = [
        {
            "type": "cdp-response-chunk",
            "chunkId": "chunk-oo",
            "seq": i,
            "total": total,
            "payload": full[i * chunk_size : (i + 1) * chunk_size],
        }
        for i in range(total)
    ]
    # Send in reverse order
    for chunk in reversed(chunks):
        await dc.handlers["message"](json.dumps(chunk))

    assert len(received) == 1
    assert received[0] == original


@pytest.mark.asyncio
async def test_dc_incomplete_chunk_set_not_forwarded():
    """An incomplete chunk set must not reach on_cdp_message."""
    t, dc = _make_transport_with_message_handler()
    received: list[dict[str, Any]] = []

    async def capture(msg: dict[str, Any]) -> None:
        received.append(msg)

    t.on_cdp_message = capture

    full = json.dumps({"id": 1, "result": {"data": "z" * 120_000}})
    chunk_size = 48000
    total = (len(full) + chunk_size - 1) // chunk_size

    # Send only the first two fragments of a multi-chunk message
    for i in range(min(2, total - 1)):
        chunk = {
            "type": "cdp-response-chunk",
            "chunkId": "chunk-incomplete",
            "seq": i,
            "total": total,
            "payload": full[i * chunk_size : (i + 1) * chunk_size],
        }
        await dc.handlers["message"](json.dumps(chunk))

    assert received == []
    assert len(t._pending_chunks) == 1  # buffered, awaiting remaining fragments


@pytest.mark.asyncio
async def test_dc_malformed_chunk_dropped():
    """A malformed chunk message is dropped without touching the buffer."""
    t, dc = _make_transport_with_message_handler()
    received: list[dict[str, Any]] = []

    async def capture(msg: dict[str, Any]) -> None:
        received.append(msg)

    t.on_cdp_message = capture

    bad = {"type": "cdp-response-chunk", "seq": 0, "total": 2}  # no chunkId/payload
    await dc.handlers["message"](json.dumps(bad))

    assert received == []
    assert t._pending_chunks == {}

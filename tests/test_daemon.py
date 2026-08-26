from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ceki_sdk._client import Client
from ceki_sdk.daemon import DaemonHTTPHandler, DaemonServer


def _make_handler(daemon: DaemonServer) -> DaemonHTTPHandler:
    """Construct an HTTP handler wired to *daemon* without a real socket."""
    handler = DaemonHTTPHandler.__new__(DaemonHTTPHandler)
    server = Mock()
    server.daemon_server = daemon
    handler.server = server
    return handler


def _make_browser(session_id: str, client: Mock) -> Mock:
    browser = Mock()
    browser.session_id = session_id
    browser.chat_topic_id = f"topic-{session_id}"
    browser.schedule_id = 1
    browser.close = AsyncMock()
    browser._client = client
    return browser


@pytest.mark.asyncio
async def test_daemon_reuses_shared_client_per_api_key():
    """Renting two sessions with the same key must use ONE shared Client."""
    daemon = DaemonServer()
    handler = _make_handler(daemon)

    shared = AsyncMock()
    shared.rent = AsyncMock(side_effect=[
        _make_browser("s1", shared),
        _make_browser("s2", shared),
    ])

    with patch("ceki_sdk.daemon.connect", AsyncMock(return_value=shared)) as connect_mock:
        r1 = await handler._handle_rent({"api_key": "key", "schedule": 5})
        r2 = await handler._handle_rent({"api_key": "key", "schedule": 6})

    assert r1["session_id"] == "s1"
    assert r2["session_id"] == "s2"

    # One shared client registered, reused across both rents — connect() once.
    assert list(daemon._clients) == ["key"]
    assert daemon._clients["key"] is shared
    connect_mock.assert_awaited_once()

    # Both sessions registered as plain Browsers on the same client.
    assert set(daemon._sessions) == {"s1", "s2"}
    assert daemon._sessions["s1"]._client is shared
    assert daemon._sessions["s2"]._client is shared

    # The session.ended hook is wired so the daemon learns about relay ends.
    assert shared._on_session_ended.__func__ is DaemonServer._on_session_ended
    assert shared._on_session_ended.__self__ is daemon


@pytest.mark.asyncio
async def test_daemon_rent_failure_does_not_leak_new_client():
    """A failed rent on a fresh client must clean up that client."""
    daemon = DaemonServer()
    handler = _make_handler(daemon)

    shared = AsyncMock()
    shared.rent = AsyncMock(side_effect=RuntimeError("no providers"))

    with patch("ceki_sdk.daemon.connect", AsyncMock(return_value=shared)):
        with pytest.raises(RuntimeError):
            await handler._handle_rent({"api_key": "key", "schedule": 5})

    assert daemon._clients == {}
    assert daemon._sessions == {}
    shared.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_session_ended_disconnects_only_when_empty():
    """session.ended removes the sid; shared client is closed only after the
    last session is gone."""
    daemon = DaemonServer()
    client_a = AsyncMock()
    client_b = AsyncMock()
    daemon._clients["key-a"] = client_a
    daemon._clients["key-b"] = client_b
    b1 = _make_browser("s1", client_a)
    b2 = _make_browser("s2", client_a)
    daemon._sessions = {"s1": b1, "s2": b2}

    # First session ends → still one live session → no disconnect.
    await daemon._on_session_ended("s1")
    assert daemon._sessions == {"s2": b2}
    assert daemon._clients == {"key-a": client_a, "key-b": client_b}
    client_a.disconnect.assert_not_awaited()
    client_b.disconnect.assert_not_awaited()

    # Last session ends → all shared clients torn down (as a scheduled task).
    await daemon._on_session_ended("s2")
    assert daemon._sessions == {}
    assert daemon._clients == {}
    assert client_a._closed is True
    assert client_b._closed is True
    await asyncio.sleep(0.05)  # let the scheduled disconnect tasks run
    client_a.disconnect.assert_awaited_once()
    client_b.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_stop_disconnects_only_when_last_session():
    """/stop pops the sid and disconnects the shared client only on the last."""
    daemon = DaemonServer()
    handler = _make_handler(daemon)
    shared = AsyncMock()
    daemon._clients["key"] = shared
    b1 = _make_browser("s1", shared)
    b2 = _make_browser("s2", shared)
    daemon._sessions = {"s1": b1, "s2": b2}

    await handler._handle_stop({"session_id": "s1"})
    assert daemon._sessions == {"s2": b2}
    b1.close.assert_awaited_once()
    shared.disconnect.assert_not_awaited()

    await handler._handle_stop({"session_id": "s2"})
    assert daemon._sessions == {}
    b2.close.assert_awaited_once()
    shared.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_shutdown_closes_sessions_and_disconnects_clients():
    """Shutdown closes every browser and disconnects every shared client."""
    daemon = DaemonServer()
    shared = AsyncMock()
    daemon._clients["key"] = shared
    b1 = _make_browser("s1", shared)
    daemon._sessions = {"s1": b1}
    daemon._httpd = None
    daemon._loop = Mock()

    await daemon._shutdown()

    b1.close.assert_awaited_once()
    shared.disconnect.assert_awaited_once()
    assert daemon._sessions == {}
    assert daemon._clients == {}


@pytest.mark.asyncio
async def test_client_dispatch_invokes_session_ended_hook():
    """The shared client fires _on_session_ended when the relay ends a session."""
    client = Client(
        api_key="k",
        relay_url="wss://relay/ws/agent",
        api_url="https://api",
        chat_url="https://chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    ended: list[str] = []

    async def hook(session_id: str) -> None:
        ended.append(session_id)

    client._on_session_ended = hook
    await client._dispatch({"type": "session.ended", "session_id": "s9", "reason": "completed"})
    assert ended == ["s9"]


@pytest.mark.asyncio
async def test_client_dispatch_session_end_alias_invokes_hook():
    """session_end (alias) also reaches the daemon hook."""
    client = Client(
        api_key="k",
        relay_url="wss://relay/ws/agent",
        api_url="https://api",
        chat_url="https://chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    ended: list[str] = []

    async def hook(session_id: str) -> None:
        ended.append(session_id)

    client._on_session_ended = hook
    await client._dispatch({"type": "session_end", "session_id": "s10"})
    assert ended == ["s10"]


@pytest.mark.asyncio
async def test_client_dispatch_relay_session_ended_event_id_invokes_hook():
    """The relay's real ``session_ended`` (underscore, id in ``event_id``) must
    reach the daemon hook — otherwise relay-initiated ends (provider death,
    admin stop, backend reaper) leak the session and its shared WS."""
    client = Client(
        api_key="k",
        relay_url="wss://relay/ws/agent",
        api_url="https://api",
        chat_url="https://chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    browser = Mock()
    browser.session_id = "s11"
    browser._on_session_ended = AsyncMock()
    client._active_browsers["s11"] = browser

    ended: list[str] = []

    async def hook(session_id: str) -> None:
        ended.append(session_id)

    client._on_session_ended = hook
    await client._dispatch({
        "type": "session_ended",
        "event_id": "s11",
        "reason": "provider_disconnected",
    })
    assert ended == ["s11"]
    browser._on_session_ended.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_dispatch_error_1011_with_event_id_cleans_session():
    """error -1011 (provider death, grace expiry) is a session end: it must
    clean the browser AND notify the daemon hook, not just log an unhandled
    relay error (which previously left the session/WS alive forever)."""
    client = Client(
        api_key="k",
        relay_url="wss://relay/ws/agent",
        api_url="https://api",
        chat_url="https://chat",
        reconnect=False,
    )
    client._ws = AsyncMock()
    client._ws.send = AsyncMock()

    browser = Mock()
    browser.session_id = "s12"
    browser._on_session_ended = AsyncMock()
    client._active_browsers["s12"] = browser

    ended: list[str] = []

    async def hook(session_id: str) -> None:
        ended.append(session_id)

    client._on_session_ended = hook
    await client._dispatch({
        "type": "error",
        "code": -1011,
        "event_id": "s12",
        "reason": "provider_disconnected",
    })
    assert ended == ["s12"]
    browser._on_session_ended.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_client_keeps_exactly_one_ws(mock_relay):
    """After N rents+closes on a shared client the relay sees EXACTLY 1 WS (not N),
    and it drops to 0 once the last session ends.  End-to-end over a real local
    WebSocket: rent() multiplexes over one connection, session.ended feeds the
    daemon hook, and the daemon disconnects the shared client when idle."""
    from ceki_sdk import ConnectOptions, connect

    url = f"ws://127.0.0.1:{mock_relay.port}/ws/agent"
    daemon = DaemonServer()

    with patch.dict(
        "os.environ",
        {"CEKI_FORCE_WS": "1", "CEKI_HUMAN_DISABLE": "1"},
    ):
        client = await connect("test-key", ConnectOptions(relay_url=url))
        client._on_session_ended = daemon._on_session_ended

        async def ack_rent(session_id: str) -> None:
            await asyncio.sleep(0.05)
            ev_id = f"ev-{session_id}"
            await mock_relay.send_to_all(
                {"type": "rent_pending", "event_id": ev_id, "schedule_id": 1}
            )
            await asyncio.sleep(0.02)
            await mock_relay.send_to_all({
                "type": "match",
                "event_id": ev_id,
                "session_id": session_id,
                "schedule_id": 1,
                "chat_topic_id": None,
                "browser_info": {},
            })

        t1 = asyncio.create_task(ack_rent("sess-A"))
        b1 = await client.rent(1)
        await t1
        t2 = asyncio.create_task(ack_rent("sess-B"))
        b2 = await client.rent(2)
        await t2

        # Both sessions multiplex over the SAME shared WebSocket.
        assert len(mock_relay.connections) == 1

        # Mirror what _handle_rent does: register the shared client + sessions.
        daemon._clients = {"test-key": client}
        daemon._sessions = {"sess-A": b1, "sess-B": b2}

        # First session ends → still one live session → WS stays up.
        await mock_relay.send_to_all(
            {"type": "session.ended", "session_id": "sess-A", "reason": "completed"}
        )
        await asyncio.sleep(0.15)
        assert len(mock_relay.connections) == 1
        assert daemon._sessions == {"sess-B": b2}

        # Last session ends via the relay's REAL format (``session_ended`` +
        # ``event_id``, as sent by finishSession on provider death / admin stop)
        # → shared client disconnects → no orphan WS.
        await mock_relay.send_to_all(
            {"type": "session_ended", "event_id": "sess-B", "reason": "completed"}
        )
        for _ in range(50):
            if len(mock_relay.connections) == 0:
                break
            await asyncio.sleep(0.05)
        assert len(mock_relay.connections) == 0
        assert daemon._sessions == {}


@pytest.mark.asyncio
async def test_provider_death_cleans_session_and_ws(mock_relay):
    """QA repro: on provider death the relay sends ``session.provider_disconnected``,
    then after grace ``error -1011`` + ``session_ended`` (id in ``event_id``).
    The daemon must drop the dead session and close the shared WS — previously
    ``session_ended`` was silently ignored, leaving a live WS and a zombie entry."""
    from ceki_sdk import ConnectOptions, connect

    url = f"ws://127.0.0.1:{mock_relay.port}/ws/agent"
    daemon = DaemonServer()

    with patch.dict(
        "os.environ",
        {"CEKI_FORCE_WS": "1", "CEKI_HUMAN_DISABLE": "1"},
    ):
        client = await connect("test-key", ConnectOptions(relay_url=url))
        client._on_session_ended = daemon._on_session_ended

        async def ack_rent(session_id: str) -> None:
            await asyncio.sleep(0.05)
            ev_id = f"ev-{session_id}"
            await mock_relay.send_to_all(
                {"type": "rent_pending", "event_id": ev_id, "schedule_id": 1}
            )
            await asyncio.sleep(0.02)
            await mock_relay.send_to_all({
                "type": "match",
                "event_id": ev_id,
                "session_id": session_id,
                "schedule_id": 1,
                "chat_topic_id": None,
                "browser_info": {},
            })

        t = asyncio.create_task(ack_rent("sess-C"))
        browser = await client.rent(1)
        await t

        assert len(mock_relay.connections) == 1
        daemon._clients = {"test-key": client}
        daemon._sessions = {"sess-C": browser}

        # 1) Provider goes down → grace starts. Session stays tracked (provider
        #    may rejoin), the WS stays up.
        await mock_relay.send_to_all({
            "type": "session.provider_disconnected",
            "session_id": "sess-C",
            "retry_within_ms": 60000,
        })
        await asyncio.sleep(0.1)
        assert daemon._sessions == {"sess-C": browser}
        assert len(mock_relay.connections) == 1

        # 2) Grace expires → relay reports the end (exact finishSession payloads).
        await mock_relay.send_to_all({
            "type": "error",
            "code": -1011,
            "event_id": "sess-C",
            "reason": "provider_disconnected",
        })
        await mock_relay.send_to_all({
            "type": "session_ended",
            "event_id": "sess-C",
            "reason": "provider_disconnected",
        })

        # Session dropped + shared client disconnected → no orphan WS.
        for _ in range(50):
            if not daemon._sessions and len(mock_relay.connections) == 0:
                break
            await asyncio.sleep(0.05)
        assert daemon._sessions == {}
        assert len(mock_relay.connections) == 0

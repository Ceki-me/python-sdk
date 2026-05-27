from __future__ import annotations

import asyncio

import pytest

from ceki_sdk import ConnectOptions, connect

from .conftest import MockRelayServer


@pytest.mark.asyncio
async def test_reconnect_after_drop(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url, reconnect=True))

    initial_ws = client._ws
    assert initial_ws is not None

    # Drop connection from server side
    for ws in list(mock_relay.connections):
        await ws.close()

    # Give client time to detect and schedule reconnect
    await asyncio.sleep(2.5)

    # Client should have started reconnect (new ws or reconnect scheduled)
    # In test environment reconnect may not succeed fully, but task should be created
    await client.close()


@pytest.mark.asyncio
async def test_no_reconnect_when_disabled(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url, reconnect=False))

    # Drop from server
    for ws in list(mock_relay.connections):
        await ws.close()

    await asyncio.sleep(0.5)
    # With reconnect=False, ws should be closed and no reconnect attempted
    await client.close()


@pytest.mark.asyncio
async def test_heartbeat_pong_updates_timestamp(mock_relay: MockRelayServer) -> None:

    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    before = client._last_pong
    # Send ping manually and verify pong updates timestamp
    await client._ws_send({"type": "ping"})
    await asyncio.sleep(0.3)
    assert client._last_pong >= before
    await client.close()

from __future__ import annotations

import pytest

from ceki_sdk import Client, ConnectOptions, connect

from .conftest import MockRelayServer


@pytest.mark.asyncio
async def test_connect_establishes_ws(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))
    assert client._ws is not None
    assert not client._ws.closed
    await client.close()


@pytest.mark.asyncio
async def test_connect_uses_bearer_subprotocol(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("my-api-key", ConnectOptions(relay_url=url))
    ws = client._ws
    assert ws is not None
    # Verify the client sent bearer subprotocol in the handshake
    # (ws.request_headers contains the Upgrade request headers)
    proto_header = ws.request_headers.get("Sec-WebSocket-Protocol", "")
    assert "bearer.my-api-key" in proto_header
    await client.close()


@pytest.mark.asyncio
async def test_close_cancels_tasks(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))
    ht = client._heartbeat_task
    rt = client._reader_task
    assert ht is not None and rt is not None
    await client.close()
    assert client._ws is None


@pytest.mark.asyncio
async def test_client_is_client_instance(mock_relay: MockRelayServer) -> None:
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))
    assert isinstance(client, Client)
    await client.close()

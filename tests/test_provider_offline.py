from __future__ import annotations

import asyncio

import pytest

from ceki_sdk import ConnectOptions, connect
from ceki_sdk._exceptions import ProviderOffline

from .conftest import MockRelayServer


@pytest.mark.asyncio
async def test_rent_error_provider_offline_raises_provider_offline(
    mock_relay: MockRelayServer,
) -> None:
    """relay sends rent.error provider_offline after probe timeout."""
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(browser_id=55))
    await asyncio.sleep(0.05)

    # Relay sends rent_pending (moves fut to _pending_rents)
    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "555", "browser_id": 55})
    await asyncio.sleep(0.05)

    # Relay sends rent.error provider_offline (with event_id, as relay does after probe timeout)
    await mock_relay.send_to_all({
        "type": "rent.error",
        "code": "provider_offline",
        "message": "Provider not responding",
        "event_id": "555",
    })

    with pytest.raises(ProviderOffline):
        await asyncio.wait_for(rent_task, timeout=5)

    await client.close()


@pytest.mark.asyncio
async def test_rent_error_provider_offline_without_event_id(
    mock_relay: MockRelayServer,
) -> None:
    """rent.error provider_offline without event_id (before rent_pending)."""
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect("testkey", ConnectOptions(relay_url=url))

    rent_task = asyncio.create_task(client.rent(browser_id=55))
    await asyncio.sleep(0.05)

    # rent.error arrives before rent_pending (fut still in queue)
    await mock_relay.send_to_all({
        "type": "rent.error",
        "code": "provider_offline",
        "message": "Provider not responding",
    })

    with pytest.raises(ProviderOffline):
        await asyncio.wait_for(rent_task, timeout=5)

    await client.close()

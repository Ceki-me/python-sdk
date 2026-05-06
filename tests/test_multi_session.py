from __future__ import annotations

import asyncio

import pytest

from ceki_browser import connect


@pytest.mark.asyncio
async def test_two_sessions_routed_independently(mock_relay):
    client = await connect("test-key", relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent")
    acked: set[str] = set()

    async def ack_rent(session_id: str, schedule_id: int) -> None:
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            rent = next(
                (m for m in mock_relay.received
                 if m.get("type") == "rent" and m.get("schedule_id") == schedule_id
                 and schedule_id not in acked),
                None,
            )
            if rent:
                acked.add(schedule_id)
                ev_id = f"ev-{session_id}"
                await mock_relay.send_to_all({"type": "rent_pending", "event_id": ev_id, "schedule_id": schedule_id})
                await asyncio.sleep(0.02)
                await mock_relay.send_to_all({
                    "type": "match",
                    "event_id": ev_id,
                    "session_id": session_id,
                    "schedule_id": schedule_id,
                    "chat_topic_id": None,
                    "browser_info": {},
                })
                return

    t1 = asyncio.create_task(ack_rent("sess-1", 1))
    t2 = asyncio.create_task(ack_rent("sess-2", 2))
    b1 = await client.rent(1)
    b2 = await client.rent(2)
    await asyncio.gather(t1, t2)

    assert b1.session_id != b2.session_id
    assert b1.session_id == "sess-1"
    assert b2.session_id == "sess-2"

    results: dict[str, dict] = {}

    async def send_and_store(browser, key):
        async def reply():
            await asyncio.sleep(0.05)
            cdp_msgs = [
                m for m in mock_relay.received
                if m.get("type") == "cdp" and m.get("session_id") == browser.session_id
            ]
            if cdp_msgs:
                await mock_relay.send_to_all({
                    "type": "cdp_response",
                    "session_id": browser.session_id,
                    "id": cdp_msgs[-1]["id"],
                    "ok": True,
                    "result": {"session": browser.session_id},
                })

        t = asyncio.create_task(reply())
        cdp = {"method": "Runtime.evaluate", "params": {"expression": "1"}}
        result = await browser.send(cdp, timeout=2)
        await t
        results[key] = result

    await asyncio.gather(
        send_and_store(b1, "b1"),
        send_and_store(b2, "b2"),
    )

    assert results["b1"]["session"] == "sess-1"
    assert results["b2"]["session"] == "sess-2"

    await b1.close()
    assert b2.session_id in client._active_browsers

    await client.close()


@pytest.mark.asyncio
async def test_close_one_session_leaves_other_alive(mock_relay):
    client = await connect("test-key", relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent")

    async def ack_rent(session_id):
        await asyncio.sleep(0.05)
        ev_id = f"ev-{session_id}"
        await mock_relay.send_to_all({"type": "rent_pending", "event_id": ev_id, "schedule_id": 1})
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
    await client.rent(1)
    await t2

    async def ack_session_end(session_id):
        await asyncio.sleep(0.05)
        await mock_relay.send_to_all({
            "type": "session.ended",
            "session_id": session_id,
            "reason": "user_stop",
        })

    t = asyncio.create_task(ack_session_end("sess-A"))
    await b1.close()
    await t

    assert "sess-A" not in client._active_browsers
    assert "sess-B" in client._active_browsers

    await client.close()

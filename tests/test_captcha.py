from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ceki_browser import Client, ConnectOptions, connect
from ceki_browser._captcha import CaptchaResult
from ceki_browser._exceptions import CaptchaTimeoutError

from .conftest import MockRelayServer

MOCK_EVENT_STORE_RESPONSE = {"id": 9001, "status_id": 100}


def _patch_httpx_post(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = status_code < 400
    resp.json = Mock(return_value=json_body or MOCK_EVENT_STORE_RESPONSE)
    resp.raise_for_status = Mock()
    client_mock = AsyncMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    client_mock.post = AsyncMock(return_value=resp)
    client_mock.patch = AsyncMock(return_value=resp)
    return patch("httpx.AsyncClient", return_value=client_mock)


async def _setup_browser(mock_relay: MockRelayServer):
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect(
        "testkey",
        ConnectOptions(relay_url=url, api_url="http://localhost:9999"),
    )
    rent_task = asyncio.create_task(client.rent(schedule_id=42))
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "500", "schedule_id": 42})
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "500",
        "session_id": "sess-123",
        "schedule_id": 42,
        "chat_topic_id": "topic-1",
        "provider_user_id": 77,
    })
    browser = await asyncio.wait_for(rent_task, timeout=5)
    return client, browser


@pytest.mark.asyncio
async def test_request_captcha_happy_path(mock_relay: MockRelayServer) -> None:
    client, browser = await _setup_browser(mock_relay)
    try:
        with _patch_httpx_post():
            captcha_task = asyncio.create_task(
                browser.request_captcha(acceptance_timeout=30, completion_timeout=30, auto_accept=False)
            )
            await asyncio.sleep(0.1)

            await mock_relay.send_to_all({
                "type": "chat.message",
                "session_id": "sess-123",
                "payload": {
                    "message": {
                        "type": "action",
                        "_id": "msg-1",
                        "topic_id": "topic-1",
                        "created_at": "2025-01-01T00:00:00Z",
                        "action": {
                            "kind": "human_action_accepted",
                            "event_id": 9001,
                            "data": {},
                        },
                    }
                },
            })
            await asyncio.sleep(0.1)

            await mock_relay.send_to_all({
                "type": "chat.message",
                "session_id": "sess-123",
                "payload": {
                    "message": {
                        "type": "action",
                        "_id": "msg-2",
                        "topic_id": "topic-1",
                        "created_at": "2025-01-01T00:00:10Z",
                        "action": {
                            "kind": "human_action_completed",
                            "event_id": 9001,
                            "data": {
                                "proof_message_id": "proof-abc",
                                "correction_id": 5555,
                            },
                        },
                    }
                },
            })

            result = await asyncio.wait_for(captcha_task, timeout=5)
            assert result.solved is True
            assert result.proof_message_id == "proof-abc"
            assert result.child_event_id == 9001
            assert result.cancel_reason is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_acceptance_timeout(mock_relay: MockRelayServer) -> None:
    client, browser = await _setup_browser(mock_relay)
    try:
        with _patch_httpx_post():
            with pytest.raises(CaptchaTimeoutError) as exc_info:
                await asyncio.wait_for(
                    browser.request_captcha(
                        acceptance_timeout=30, completion_timeout=30, auto_accept=False,
                    ),
                    timeout=35,
                )
            assert exc_info.value.phase == "acceptance"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provider_declined(mock_relay: MockRelayServer) -> None:
    client, browser = await _setup_browser(mock_relay)
    try:
        with _patch_httpx_post():
            captcha_task = asyncio.create_task(
                browser.request_captcha(acceptance_timeout=30, completion_timeout=30, auto_accept=False)
            )
            await asyncio.sleep(0.1)

            await mock_relay.send_to_all({
                "type": "chat.message",
                "session_id": "sess-123",
                "payload": {
                    "message": {
                        "type": "action",
                        "_id": "msg-d",
                        "topic_id": "topic-1",
                        "created_at": "2025-01-01T00:00:00Z",
                        "action": {
                            "kind": "human_action_declined",
                            "event_id": 9001,
                            "data": {},
                        },
                    }
                },
            })

            result = await asyncio.wait_for(captcha_task, timeout=5)
            assert result.solved is False
            assert result.cancel_reason == "declined"
            assert result.child_event_id == 9001
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_min_timeout_hard_30s(mock_relay: MockRelayServer) -> None:
    client, browser = await _setup_browser(mock_relay)
    try:
        with pytest.raises(ValueError, match="acceptance_timeout must be >= 30"):
            await browser.request_captcha(acceptance_timeout=20)
        with pytest.raises(ValueError, match="completion_timeout must be >= 30"):
            await browser.request_captcha(completion_timeout=10)
    finally:
        await client.close()

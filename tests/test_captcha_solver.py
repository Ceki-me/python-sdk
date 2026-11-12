from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ceki_sdk import ConnectOptions, connect
from ceki_sdk._captcha_solver import CaptchaSolver, detect_captcha, inject_solution
from ceki_sdk._exceptions import CaptchaError

from .conftest import MockRelayServer


def _mock_http(*responses):
    """Build an AsyncClient mock whose post() yields responses in sequence."""
    client_mock = AsyncMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    client_mock.post = AsyncMock(side_effect=responses)
    return client_mock


def _resp(status_code=200, json_body=None):
    r = Mock()
    r.status_code = status_code
    r.is_success = status_code < 400
    r.text = ""
    r.json = Mock(return_value=json_body or {})
    return r


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("CEKI_2CAPTCHA_API_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_API_KEY", raising=False)


def test_requires_key(no_key):
    with pytest.raises(CaptchaError, match="CEKI_2CAPTCHA_API_KEY"):
        CaptchaSolver()


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("CEKI_2CAPTCHA_API_KEY", "secret-key")
    s = CaptchaSolver()
    assert s._api_key == "secret-key"


def test_legacy_twocaptcha_env(monkeypatch):
    monkeypatch.delenv("CEKI_2CAPTCHA_API_KEY", raising=False)
    monkeypatch.setenv("TWOCAPTCHA_API_KEY", "legacy-key")
    s = CaptchaSolver()
    assert s._api_key == "legacy-key"


@pytest.mark.asyncio
async def test_solve_happy_path():
    create_resp = _resp(json_body={"taskId": "12345"})
    processing_resp = _resp(json_body={"status": "processing"})
    ready_resp = _resp(json_body={
        "status": "ready",
        "solution": {"gRecaptchaResponse": "P0_123"},
    })
    http_mock = _mock_http(create_resp, processing_resp, ready_resp)

    with patch("httpx.AsyncClient", return_value=http_mock) as cls:
        solver = CaptchaSolver("key", poll_timeout=5, poll_interval=0.01)
        result = await solver.solve(
            task_type="HCaptchaTaskProxyless",
            website_url="https://chat.deepseek.com/sign_up",
            website_key="site-key-abc",
        )

    assert result.solved is True
    assert result.token == "P0_123"
    assert result.task_type == "HCaptchaTaskProxyless"
    assert result.solver_response["status"] == "ready"

    # Verify the createTask payload shape.
    post_args = [c for c in cls.return_value.post.call_args_list]
    create_call = post_args[0]
    body = create_call.kwargs.get("json", {})
    assert body["clientKey"] == "key"
    assert body["task"]["type"] == "HCaptchaTaskProxyless"
    assert body["task"]["websiteKey"] == "site-key-abc"


@pytest.mark.asyncio
async def test_solve_timeout():
    create_resp = _resp(json_body={"taskId": "999"})
    # Always return "processing" for getTaskResult — the solver must keep
    # polling until the timeout; createTask returns a taskId.
    processing_resp = _resp(json_body={"status": "processing"})
    http_mock = AsyncMock()
    http_mock.__aenter__ = AsyncMock(return_value=http_mock)
    http_mock.__aexit__ = AsyncMock(return_value=False)

    async def _post(url, *args, **kwargs):
        return create_resp if url.endswith("createTask") else processing_resp
    http_mock.post = _post

    with patch("httpx.AsyncClient", return_value=http_mock):
        solver = CaptchaSolver("key", poll_timeout=0.05, poll_interval=0.01)
        result = await solver.solve(
            task_type="RecaptchaV3TaskProxyless",
            website_url="https://example.com",
            website_key="k2",
        )

    assert result.solved is False
    assert "timeout" in result.error


@pytest.mark.asyncio
async def test_solve_unsolvable_retries_once():
    create1 = _resp(json_body={"taskId": "1"})
    unsolvable = _resp(json_body={"status": "failed", "error": "ERROR_CAPTCHA_UNSOLVABLE"})
    create2 = _resp(json_body={"taskId": "2"})
    ready = _resp(json_body={"status": "ready", "solution": {"gRecaptchaResponse": "T2"}})
    http_mock = _mock_http(create1, unsolvable, create2, ready)

    with patch("httpx.AsyncClient", return_value=http_mock) as cls:
        solver = CaptchaSolver("key", poll_timeout=5, poll_interval=0.01)
        result = await solver.solve(
            task_type="HCaptchaTaskProxyless",
            website_url="https://example.com",
            website_key="k",
        )

    assert result.solved is True
    assert result.token == "T2"
    # two createTask calls happened
    creates = [
        c for c in cls.return_value.post.call_args_list
        if "createTask" in str(c)
    ]
    assert len(creates) == 2


@pytest.mark.asyncio
async def test_solve_fatal_error_raises():
    create_resp = _resp(status_code=200, json_body={"error": "ERROR_ZERO_BALANCE"})
    http_mock = _mock_http(create_resp)

    with patch("httpx.AsyncClient", return_value=http_mock):
        solver = CaptchaSolver("key")
        with pytest.raises(CaptchaError, match="ERROR_ZERO_BALANCE"):
            await solver.solve(
                task_type="HCaptchaTaskProxyless",
                website_url="https://example.com",
                website_key="k",
            )


@pytest.mark.asyncio
async def test_solve_http_error_raises():
    create_resp = _resp(status_code=500, json_body={})
    http_mock = _mock_http(create_resp)

    with patch("httpx.AsyncClient", return_value=http_mock):
        solver = CaptchaSolver("key")
        with pytest.raises(CaptchaError, match="HTTP 500"):
            await solver.solve(
                task_type="HCaptchaTaskProxyless",
                website_url="https://example.com",
                website_key="k",
            )


@pytest.mark.asyncio
async def test_solve_missing_token_in_ready():
    ready_resp = _resp(json_body={"status": "ready", "solution": {}})
    create_resp = _resp(json_body={"taskId": "1"})
    http_mock = _mock_http(create_resp, ready_resp)

    with patch("httpx.AsyncClient", return_value=http_mock):
        solver = CaptchaSolver("key")
        result = await solver.solve(
            task_type="HCaptchaTaskProxyless",
            website_url="https://example.com",
            website_key="k",
        )

    assert result.solved is False
    assert "no token" in result.error


async def _setup_browser(mock_relay: MockRelayServer):
    url = f"ws://127.0.0.1:{mock_relay.port}"
    client = await connect(
        "testkey",
        ConnectOptions(relay_url=url, api_url="http://localhost:9999"),
    )
    rent_task = asyncio.create_task(client.rent(schedule_id=42))
    await mock_relay.wait_for_message("rent")
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


@pytest.fixture(autouse=True)
def _force_ws_transport(monkeypatch):
    monkeypatch.setenv("CEKI_FORCE_WS", "1")


@pytest.mark.asyncio
async def test_detect_captcha_hcaptcha(mock_relay: MockRelayServer):
    client, browser = await _setup_browser(mock_relay)
    try:
        # The extension returns the detect JSON as evaluate value.
        async def fake_send(cdp, **kw):
            expr = cdp["params"]["expression"]
            if cdp["method"] == "Runtime.evaluate" and "document.querySelector('.h-captcha" in expr:
                return {
                    "result": {
                        "value": '{"found": true, "type": "hcaptcha", "key": "sitekey-1"}'
                    }
                }
            return {"result": {"value": ""}}
        browser.send = fake_send

        detected = await detect_captcha(browser)
        assert detected == {"found": True, "type": "hcaptcha", "key": "sitekey-1"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_detect_captcha_recaptcha_iframe(mock_relay: MockRelayServer):
    client, browser = await _setup_browser(mock_relay)
    try:
        async def fake_send(cdp, **kw):
            if cdp["method"] == "Runtime.evaluate":
                return {
                    "result": {
                        "value": '{"found": true, "type": "recaptcha", "key": "6Lc-key"}'
                    }
                }
            return {"result": {"value": ""}}
        browser.send = fake_send

        detected = await detect_captcha(browser)
        assert detected["found"] is True
        assert detected["type"] == "recaptcha"
        assert detected["key"] == "6Lc-key"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_detect_captcha_not_found(mock_relay: MockRelayServer):
    client, browser = await _setup_browser(mock_relay)
    try:
        async def fake_send(cdp, **kw):
            return {"result": {"value": '{"found": false}'}}
        browser.send = fake_send

        detected = await detect_captcha(browser)
        assert detected["found"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_inject_solution_hcaptcha(mock_relay: MockRelayServer):
    client, browser = await _setup_browser(mock_relay)
    try:
        async def fake_send(cdp, **kw):
            assert cdp["method"] == "Runtime.evaluate"
            expr = cdp["params"]["expression"]
            assert "h-captcha-response" in expr
            assert '"P0_token"' in expr  # token escaped into JS string
            return {"result": {"value": '{"injected": true, "hasResponse": true}'}}
        browser.send = fake_send

        result = await inject_solution(browser, "HCaptchaTaskProxyless", "P0_token")
        assert result["injected"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_solve_captcha_no_key(mock_relay: MockRelayServer, no_key):
    client, browser = await _setup_browser(mock_relay)
    try:
        with pytest.raises(CaptchaError, match="CEKI_2CAPTCHA_API_KEY"):
            await browser.solve_captcha()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_solve_captcha_no_widget(mock_relay: MockRelayServer, monkeypatch):
    monkeypatch.setenv("CEKI_2CAPTCHA_API_KEY", "key")
    client, browser = await _setup_browser(mock_relay)
    try:
        async def fake_send(cdp, **kw):
            expr = cdp["params"]["expression"]
            if "document.querySelector('.h-captcha" in expr:
                return {"result": {"value": '{"found": false}'}}
            if "location.href" in expr:
                return {"result": {"value": "https://example.com"}}
            return {"result": {"value": ""}}
        browser.send = fake_send

        result = await browser.solve_captcha()
        assert result.solved is False
        assert "no captcha widget" in result.error
    finally:
        await client.close()

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ceki_sdk import ConnectOptions, connect
from ceki_sdk._client import Client
from ceki_sdk._profile import BrowserProfile
from ceki_sdk._vault import (
    VaultSession,
    minimal_vault_profile,
    normalize_profile_for_vault,
    sanitize_cookies,
)

from .conftest import MockRelayServer

# A decrypted vault session envelope exactly as /api/vault/sessions/{id} returns it.
SAMPLE_VAULT_DATA = {
    "fingerprint": {
        "userAgent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/153.0.0.0",
        "canvasNoise": 0.00041478621121495963,
    },
    "cookies": [
        {
            "name": "auth-refresh-remember",
            "value": "ed2679ccfa8e6b3ab8dc3ec0215363ab",
            "domain": ".vc.ru",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1796120019.910119,
            "sameSite": "Lax",
            "priority": "Medium",
            "session": False,
            "size": 85,
            "sourcePort": 443,
            "sourceScheme": "Secure",
        }
    ],
    "localStorage": {
        "https://vc.ru": {
            "user": '{"id":5537554,"name":"Kom"}',
            "auth-refresh-token": '{"token":"ed2679cc","expTimestamp":1796120019}',
        }
    },
    "sessionStorage": {"https://vc.ru": {"__ym_tab_guid": "62152f98"}},
    "urls": ["https://vc.ru/education_on_vc_ru/3163579"],
    "collectedAt": "2026-10-02T10:13:43.756Z",
}


def _make_response(status: int = 200, json_data: dict | list | None = None) -> httpx.Response:
    req = httpx.Request("GET", "http://test")
    return httpx.Response(status, json=json_data, request=req)


def _make_client(relay_url: str = "wss://relay.ceki.me/ws/agent") -> Client:
    return Client(
        api_key="testkey",
        relay_url=relay_url,
        api_url="https://api.ceki.me",
        chat_url="https://chat.ceki.me/api/chat",
        reconnect=False,
    )


# ── sanitize_cookies ─────────────────────────────────────────────────────────

def test_sanitize_cookies_drops_cdp_only_fields() -> None:
    raw = [
        {
            "name": "a",
            "value": "1",
            "domain": ".vc.ru",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1796120019.91,
            "sameSite": "Lax",
            "priority": "Medium",
            "size": 85,
            "sourcePort": 443,
            "sourceScheme": "Secure",
            "session": False,
        }
    ]
    out = sanitize_cookies(raw)
    assert out == [
        {
            "name": "a",
            "value": "1",
            "domain": ".vc.ru",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1796120019.91,
            "sameSite": "Lax",
            "session": False,
        }
    ]


def test_sanitize_cookies_skips_invalid_entries() -> None:
    raw = [
        {"name": "ok", "value": "1", "domain": ".x.com"},
        {"name": "no-value", "domain": ".x.com"},  # missing value
        {"name": "int-value", "value": 2, "domain": ".x.com"},  # non-str value
        "not-a-dict",
    ]
    out = sanitize_cookies(raw)
    assert len(out) == 1
    assert out[0]["name"] == "ok"


# ── profile envelope conversion ──────────────────────────────────────────────

def test_normalize_profile_to_vault_envelope() -> None:
    profile = {
        "schema_version": 2,
        "fingerprint": {"userAgent": "x"},
        "origin": "https://vc.ru",
        "cookies": [
            {
                "name": "a",
                "value": "1",
                "domain": ".vc.ru",
                "session": False,
                "priority": "Medium",
            }
        ],
        "localStorage": {"user": '{"id":5537554}'},
        "sessionStorage": {"__ym_tab_guid": "x"},
    }
    env = normalize_profile_for_vault(profile)
    assert env["fingerprint"] == {"userAgent": "x"}
    # extra CDP-only cookie fields dropped
    assert env["cookies"][0] == {"name": "a", "value": "1", "domain": ".vc.ru", "session": False}
    # flat storage is wrapped under the exported origin
    assert env["localStorage"] == {"https://vc.ru": {"user": '{"id":5537554}'}}
    assert env["sessionStorage"] == {"https://vc.ru": {"__ym_tab_guid": "x"}}
    assert env["urls"] == ["https://vc.ru"]
    assert env["collectedAt"]


def test_normalize_profile_missing_origin() -> None:
    env = normalize_profile_for_vault(
        {"fingerprint": None, "cookies": [], "localStorage": {}, "sessionStorage": {}}
    )
    assert env["localStorage"] == {}
    assert env["sessionStorage"] == {}
    assert env["urls"] == []


def test_normalize_profile_passes_through_per_origin_storage() -> None:
    # A re-uploaded vault envelope already has per-origin storage — the values
    # are dicts, so don't re-wrap under the flat origin key.
    env = normalize_profile_for_vault(
        {
            "origin": "https://vc.ru",
            "cookies": [],
            "fingerprint": None,
            "localStorage": {"https://vc.ru": {"user": "x"}},
            "sessionStorage": {"https://vc.ru": {"t": "y"}},
        }
    )
    assert env["localStorage"]["https://vc.ru"] == {"user": "x"}
    assert env["sessionStorage"]["https://vc.ru"] == {"t": "y"}


def test_minimal_vault_profile_passthrough_and_drop() -> None:
    prof = minimal_vault_profile(SAMPLE_VAULT_DATA)
    assert prof["cookies"] == [
        {
            "name": "auth-refresh-remember",
            "value": "ed2679ccfa8e6b3ab8dc3ec0215363ab",
            "domain": ".vc.ru",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1796120019.910119,
            "sameSite": "Lax",
            "session": False,
        }
    ]
    # per-origin storage kept intact
    assert prof["localStorage"]["https://vc.ru"]["user"] == '{"id":5537554,"name":"Kom"}'
    assert prof["sessionStorage"]["https://vc.ru"]["__ym_tab_guid"] == "62152f98"
    # fingerprint is NOT part of the extension profile — it is applied via the
    # top-level session.configure fingerprint field (single source of truth)
    assert "fingerprint" not in prof
    # urls/collectedAt are vault metadata, not extension profile fields
    assert "urls" not in prof
    assert "collectedAt" not in prof


def test_minimal_vault_profile_empty_fingerprint_omitted() -> None:
    prof = minimal_vault_profile({"cookies": [], "fingerprint": {}})
    assert "fingerprint" not in prof


# ── VaultSession parses both index and show payloads ─────────────────────────

def test_vault_session_from_index_payload() -> None:
    session = VaultSession.from_payload(
        {"id": 8, "label": "S", "created_at": "2026-10-01", "urls": ["https://vc.ru/"]}
    )
    assert session.id == 8
    assert session.data == {}
    assert session.urls == ["https://vc.ru/"]


def test_vault_session_from_show_payload_reads_urls_from_envelope() -> None:
    session = VaultSession.from_payload(
        {"id": 8, "label": "S", "user_id": 1, "data": SAMPLE_VAULT_DATA}
    )
    assert session.data["fingerprint"]["canvasNoise"] == 0.00041478621121495963
    assert session.urls == ["https://vc.ru/education_on_vc_ru/3163579"]


# ── ClientVault HTTP surface (mocked httpx.AsyncClient) ──────────────────────

@pytest.mark.asyncio
async def test_vault_list_parses_paginated_response() -> None:
    client = _make_client()
    payload = {
        "current_page": 1,
        "data": [
            {"id": 8, "label": "A", "urls": ["https://vc.ru/"]},
            {"id": 9, "label": "B", "urls": []},
        ],
    }
    mock_get = AsyncMock(return_value=_make_response(200, payload))

    with patch("httpx.AsyncClient.get", mock_get):
        result = await client.vault.list(per_page=20)

    assert len(result) == 2
    assert result[0].id == 8
    assert result[1].label == "B"
    # hit the right endpoint
    call_args = mock_get.call_args
    assert call_args[0][0] == "https://api.ceki.me/api/vault/sessions"
    assert call_args[1]["headers"]["Authorization"] == "Bearer testkey"


@pytest.mark.asyncio
async def test_vault_get_returns_decrypted_data() -> None:
    client = _make_client()
    payload = {"id": 8, "user_id": 1, "label": "S", "data": SAMPLE_VAULT_DATA}
    mock_get = AsyncMock(return_value=_make_response(200, payload))

    with patch("httpx.AsyncClient.get", mock_get):
        session = await client.vault.get(8)

    assert session.id == 8
    assert session.data["cookies"][0]["name"] == "auth-refresh-remember"
    assert session.data["localStorage"]["https://vc.ru"]["user"]
    assert session.urls == ["https://vc.ru/education_on_vc_ru/3163579"]


@pytest.mark.asyncio
async def test_vault_create_and_update_post_envelope() -> None:
    client = _make_client()
    envelope = normalize_profile_for_vault(
        {
            "fingerprint": None,
            "origin": "https://vc.ru",
            "cookies": [],
            "localStorage": {"k": "v"},
            "sessionStorage": {},
        }
    )
    mock_post = AsyncMock(return_value=_make_response(201, {"id": 42}))
    mock_put = AsyncMock(return_value=_make_response(200, {"id": 42, "data": envelope}))

    with patch("httpx.AsyncClient.post", mock_post), patch("httpx.AsyncClient.put", mock_put):
        created = await client.vault.create(envelope, label="Snapshot")
        updated = await client.vault.update(42, envelope, label="Snapshot2")

    assert created == 42
    assert updated.id == 42
    # POST sends {label, data}; PUT overwrites data
    post_body = mock_post.call_args[1]["json"]
    assert post_body["label"] == "Snapshot"
    assert post_body["data"]["urls"] == ["https://vc.ru"]
    put_body = mock_put.call_args[1]["json"]
    assert put_body["label"] == "Snapshot2"
    assert mock_put.call_args[0][0] == "https://api.ceki.me/api/vault/sessions/42"


@pytest.mark.asyncio
async def test_vault_delete() -> None:
    client = _make_client()
    mock_delete = AsyncMock(return_value=_make_response(204, None))

    with patch("httpx.AsyncClient.delete", mock_delete):
        await client.vault.delete(8)

    assert mock_delete.call_args[0][0] == "https://api.ceki.me/api/vault/sessions/8"


# ── BrowserVault sugar ──────────────────────────────────────────────────────

class FakeBrowser:
    """Minimal Browser stand-in with a mocked CDP send and a real configure."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.send = AsyncMock(return_value={"result": {"value": "{}"}})
        self.configure_calls: list[dict] = []
        self._vault_session_id: int | None = None
        self.profile = BrowserProfile(self)  # type: ignore[arg-type]
        from ceki_sdk._vault import BrowserVault
        self.vault = BrowserVault(self)  # type: ignore[arg-type]

    async def configure(self, **kwargs: dict) -> None:  # type: ignore[override]
        self.configure_calls.append(kwargs)


SAMPLE_PROFILE_EXPORT = {
    "schema_version": 2,
    "fingerprint": {"userAgent": "x"},
    "origin": "https://vc.ru",
    "cookies": [{"name": "a", "value": "1", "domain": ".vc.ru"}],
    "localStorage": {"user": '{"id":1}'},
    "sessionStorage": {},
}


@pytest.mark.asyncio
async def test_browser_vault_save_creates_new_session() -> None:
    client = _make_client()
    fb = FakeBrowser(client)
    fb.send.side_effect = [
        {"fingerprint": SAMPLE_PROFILE_EXPORT["fingerprint"]},
        {"cookies": SAMPLE_PROFILE_EXPORT["cookies"]},
        {"result": {"value": '{"user":"{\\"id\\":1}"}'}},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://vc.ru"}},
    ]
    mock_post = AsyncMock(return_value=_make_response(201, {"id": 77}))

    with patch("httpx.AsyncClient.post", mock_post):
        vid = await fb.vault.save(label="Snapshot")

    assert vid == 77
    body = mock_post.call_args[1]["json"]
    assert body["label"] == "Snapshot"
    assert body["data"]["cookies"][0]["name"] == "a"
    assert body["data"]["localStorage"]["https://vc.ru"]  # wrapped by origin


@pytest.mark.asyncio
async def test_browser_vault_save_overwrites_bound_session() -> None:
    client = _make_client()
    fb = FakeBrowser(client)
    fb.send.side_effect = [
        {"fingerprint": SAMPLE_PROFILE_EXPORT["fingerprint"]},
        {"cookies": SAMPLE_PROFILE_EXPORT["cookies"]},
        {"result": {"value": "{}"}},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://vc.ru"}},
    ]
    fb._vault_session_id = 42
    mock_put = AsyncMock(return_value=_make_response(200, {"id": 42, "data": {}}))

    with patch("httpx.AsyncClient.put", mock_put):
        vid = await fb.vault.save()

    assert vid == 42
    assert mock_put.call_args[0][0] == "https://api.ceki.me/api/vault/sessions/42"


@pytest.mark.asyncio
async def test_browser_vault_restore_sends_configure_with_profile() -> None:
    client = _make_client()
    fb = FakeBrowser(client)
    payload = {"id": 8, "data": SAMPLE_VAULT_DATA}
    mock_get = AsyncMock(return_value=_make_response(200, payload))

    with patch("httpx.AsyncClient.get", mock_get):
        await fb.vault.restore(8)

    assert fb.configure_calls, "restore must send session.configure"
    cfg = fb.configure_calls[0]
    assert cfg["profile"]["cookies"][0]["name"] == "auth-refresh-remember"
    # sanitized: no priority/size/sourcePort
    assert "priority" not in cfg["profile"]["cookies"][0]
    assert cfg["profile"]["localStorage"]["https://vc.ru"]["user"]
    # fingerprint promoted to its own config field
    assert cfg["fingerprint"]["canvasNoise"] == 0.00041478621121495963
    # bound id recorded for later save-overwrite
    assert fb._vault_session_id == 8


@pytest.mark.asyncio
async def test_browser_vault_restore_accepts_raw_envelope() -> None:
    client = _make_client()
    fb = FakeBrowser(client)
    await fb.vault.restore(SAMPLE_VAULT_DATA)
    cfg = fb.configure_calls[0]
    assert cfg["profile"]["localStorage"]["https://vc.ru"]
    assert fb._vault_session_id is None  # no API fetch → not bound


# ── rent(vault=...) end-to-end through a mocked relay ────────────────────────

@pytest.mark.asyncio
async def test_rent_with_vault_restores_profile(
    mock_relay: MockRelayServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    # Force WS transport so the test doesn't wait for P2P/WebRTC handshake.
    monkeypatch.setenv("CEKI_FORCE_WS", "1")
    url = f"ws://127.0.0.1:{mock_relay.port}"
    # vault.get() is an httpx GET — mock it so rent can restore from the API
    payload = {"id": 8, "data": SAMPLE_VAULT_DATA}
    mock_get = AsyncMock(return_value=_make_response(200, payload))

    client = await connect("testkey", ConnectOptions(relay_url=url))
    rent_task = asyncio.create_task(client.rent(schedule_id=240, vault=8))
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({"type": "rent_pending", "event_id": "v1", "schedule_id": 240})
    await asyncio.sleep(0.05)
    await mock_relay.send_to_all({
        "type": "match",
        "event_id": "v1",
        "session_id": "v1",
        "schedule_id": 240,
        "capabilities": {},
        "price_per_min": 0.01,
    })

    with patch("httpx.AsyncClient.get", mock_get):
        browser = await asyncio.wait_for(rent_task, timeout=5)

    assert browser.session_id == "v1"
    assert browser._vault_session_id == 8
    await asyncio.sleep(0.1)

    configure_msgs = [m for m in mock_relay.received if m.get("type") == "session.configure"]
    assert len(configure_msgs) == 1
    assert configure_msgs[0]["session_id"] == "v1"
    assert configure_msgs[0]["profile"]["cookies"][0]["name"] == "auth-refresh-remember"
    assert configure_msgs[0]["fingerprint"]["canvasNoise"] == 0.00041478621121495963

    await client.close()

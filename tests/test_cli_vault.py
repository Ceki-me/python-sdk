from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceki_sdk._vault import VaultSession
from ceki_sdk.cli import _cmd_vault, build_parser

# A decrypted vault envelope as GET /api/vault/sessions/{id} returns it.
SAMPLE_DATA = {
    "fingerprint": {"userAgent": "Mozilla/5.0"},
    "cookies": [
        {
            "name": "auth-refresh-remember",
            "value": "ed2679cc",
            "domain": ".vc.ru",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1796120019.91,
            "sameSite": "Lax",
            "session": False,
        }
    ],
    "localStorage": {"https://vc.ru": {"user": '{"id":1}'}},
    "sessionStorage": {},
    "urls": ["https://vc.ru/"],
    "collectedAt": "2026-10-02T10:13:43.756Z",
}


def _vault_session(**overrides):
    payload = {
        "id": 8,
        "label": "vc.ru profile",
        "user_id": 1,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
    }
    payload.update(overrides)
    return VaultSession.from_payload(payload)


# ──────────────────────────────────────────────────────────────────────────
# Parser tests
# ──────────────────────────────────────────────────────────────────────────


def test_parser_vault_list():
    args = build_parser().parse_args(["vault", "list", "--json"])
    assert args.command == "vault"
    assert args.vault_action == "list"
    assert args.json is True


def test_parser_vault_get():
    args = build_parser().parse_args(["vault", "get", "42", "--json"])
    assert args.vault_action == "get"
    assert args.id == 42
    assert args.json is True


def test_parser_vault_get_output():
    args = build_parser().parse_args(["vault", "get", "42", "-o", "/tmp/p.json"])
    assert args.output == "/tmp/p.json"


def test_parser_vault_save_path():
    args = build_parser().parse_args(["vault", "save", "/tmp/p.json"])
    assert args.vault_action == "save"
    assert args.path == "/tmp/p.json"
    assert args.session is None
    assert args.id is None


def test_parser_vault_save_session():
    args = build_parser().parse_args(["vault", "save", "--session", "sess-1", "--label", "L"])
    assert args.vault_action == "save"
    assert args.session == "sess-1"
    assert args.path is None
    assert args.label == "L"
    assert args.no_session_storage is False


def test_parser_vault_save_update():
    args = build_parser().parse_args(["vault", "save", "/tmp/p.json", "--id", "7"])
    assert args.id == 7


def test_parser_vault_apply_schedule():
    args = build_parser().parse_args(["vault", "apply", "8", "--schedule", "240"])
    assert args.vault_action == "apply"
    assert args.id == 8
    assert args.schedule == 240
    assert args.session is None


def test_parser_vault_apply_session():
    args = build_parser().parse_args(["vault", "apply", "8", "--session", "s1"])
    assert args.session == "s1"
    assert args.schedule is None


def test_parser_vault_delete():
    args = build_parser().parse_args(["vault", "delete", "8"])
    assert args.vault_action == "delete"
    assert args.id == 8


# ──────────────────────────────────────────────────────────────────────────
# Handler tests (mocked ClientVault)
# ──────────────────────────────────────────────────────────────────────────


def _mock_http_client(vault_mock: MagicMock):
    client = MagicMock()
    client.vault = vault_mock
    return client


async def _run(args: list[str], vault_mock: MagicMock):
    parsed = build_parser().parse_args(["vault", *args])
    with patch(
        "ceki_sdk.cli._vault_http_client",
        return_value=_mock_http_client(vault_mock),
    ), patch.dict("os.environ", {"CEKI_API_KEY": "testkey"}, clear=False):
        await _cmd_vault(parsed)


@pytest.mark.asyncio
async def test_vault_list_calls_api_and_outputs_summary(capsys):
    vault = MagicMock()
    vault.list = AsyncMock(return_value=[
        _vault_session(),
        _vault_session(id=9, label="Other", urls=["https://other.ru/"]),
    ])
    await _run(["list"], vault)
    vault.list.assert_awaited_once_with(per_page=20)
    out = capsys.readouterr().out
    assert "8" in out
    assert "vc.ru profile" in out
    assert "URLS" in out


@pytest.mark.asyncio
async def test_vault_list_json(capsys):
    vault = MagicMock()
    vault.list = AsyncMock(return_value=[
        _vault_session(data=SAMPLE_DATA),
    ])
    await _run(["list", "--json"], vault)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["id"] == 8
    assert parsed[0]["cookie_count"] == 1
    assert parsed[0]["storage_origins"] == 1


@pytest.mark.asyncio
async def test_vault_get_json_prints_decrypted_data(capsys):
    vault = MagicMock()
    vault.get = AsyncMock(return_value=_vault_session(data=SAMPLE_DATA))
    await _run(["get", "8", "--json"], vault)
    vault.get.assert_awaited_once_with(8)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["cookies"][0]["name"] == "auth-refresh-remember"
    assert parsed["localStorage"]["https://vc.ru"]["user"]


@pytest.mark.asyncio
async def test_vault_get_human_summary(capsys):
    vault = MagicMock()
    vault.get = AsyncMock(return_value=_vault_session(data=SAMPLE_DATA))
    await _run(["get", "8"], vault)
    out = capsys.readouterr().out
    assert "Vault session 8" in out
    assert "cookies:    1" in out
    assert "https://vc.ru/" in out


@pytest.mark.asyncio
async def test_vault_get_output_dumps_profile(tmp_path: Path, capsys):
    vault = MagicMock()
    vault.get = AsyncMock(return_value=_vault_session(data=SAMPLE_DATA))
    out_path = tmp_path / "profile.json"
    await _run(["get", "8", "-o", str(out_path)], vault)
    dumped = json.loads(out_path.read_text())
    assert dumped["cookies"][0]["name"] == "auth-refresh-remember"
    assert "Saved decrypted profile" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_vault_save_creates_new_session(tmp_path: Path, capsys):
    vault = MagicMock()
    vault.create = AsyncMock(return_value=77)
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps(SAMPLE_DATA))
    await _run(["save", str(profile), "--label", "My"], vault)
    created_call = vault.create.await_args
    assert created_call.kwargs["label"] == "My"
    # profile.export() snapshot shape gets normalized into a vault envelope
    assert "cookies" in created_call.args[0]
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "id": 77, "action": "created", "label": "My"}


@pytest.mark.asyncio
async def test_vault_save_updates_existing_session(tmp_path: Path):
    vault = MagicMock()
    vault.update = AsyncMock(return_value=_vault_session())
    vault.create = AsyncMock(return_value=0)  # must never be awaited
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps(SAMPLE_DATA))
    await _run(["save", str(profile), "--id", "8"], vault)
    updated = vault.update.await_args
    assert updated.args[0] == 8
    assert updated.args[1]["cookies"]
    assert vault.create.await_args is None


@pytest.mark.asyncio
async def test_vault_save_missing_file_raises():
    vault = MagicMock()
    with pytest.raises(FileNotFoundError):
        await _run(["save", "/does/not/exist.json"], vault)


@pytest.mark.asyncio
async def test_vault_save_requires_source():
    vault = MagicMock()
    from ceki_sdk._exceptions import CekiError
    with pytest.raises(CekiError):
        await _run(["save"], vault)


@pytest.mark.asyncio
async def test_vault_save_from_session_snapshots(capsys):
    vault = MagicMock()
    vault.create = AsyncMock(return_value=99)

    profile = {
        "schema_version": 2,
        "fingerprint": {"userAgent": "x"},
        "origin": "https://vc.ru",
        "cookies": [{"name": "a", "value": "1", "domain": ".vc.ru"}],
        "localStorage": {"user": '{"id":1}'},
        "sessionStorage": {},
    }
    browser = MagicMock()
    browser.profile.export = AsyncMock(return_value=profile)
    resume_browser = AsyncMock(return_value=(None, browser))

    parsed = build_parser().parse_args(
        ["vault", "save", "--session", "sess-1", "--label", "snap"]
    )
    with patch(
        "ceki_sdk.cli._vault_http_client",
        return_value=_mock_http_client(vault),
    ), patch("ceki_sdk.cli._resume_browser", resume_browser), patch.dict(
        "os.environ", {"CEKI_API_KEY": "testkey"}, clear=False
    ):
        await _cmd_vault(parsed)

    browser.profile.export.assert_awaited_once_with(include_session_storage=True)
    created = vault.create.await_args
    assert created.kwargs["label"] == "snap"
    assert created.args[0]["localStorage"]["https://vc.ru"]["user"]
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == 99


@pytest.mark.asyncio
async def test_vault_apply_to_existing_session(capsys):
    vault = MagicMock()
    browser = MagicMock()
    browser.session_id = "sess-1"
    browser.vault.restore = AsyncMock()
    resume_browser = AsyncMock(return_value=(None, browser))

    parsed = build_parser().parse_args(["vault", "apply", "8", "--session", "sess-1"])
    with patch(
        "ceki_sdk.cli._vault_http_client",
        return_value=_mock_http_client(vault),
    ), patch("ceki_sdk.cli._resume_browser", resume_browser), patch.dict(
        "os.environ", {"CEKI_API_KEY": "testkey"}, clear=False
    ):
        await _cmd_vault(parsed)

    browser.vault.restore.assert_awaited_once_with(8)
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_vault_apply_requires_target():
    vault = MagicMock()
    from ceki_sdk._exceptions import CekiError
    with pytest.raises(CekiError):
        await _run(["apply", "8"], vault)


@pytest.mark.asyncio
async def test_vault_delete(capsys):
    vault = MagicMock()
    vault.delete = AsyncMock()
    await _run(["delete", "8"], vault)
    vault.delete.assert_awaited_once_with(8)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "id": 8, "deleted": True}

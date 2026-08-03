from __future__ import annotations

import os

import pytest

from ceki_sdk._provider import (
    ProviderError,
    resolve_api_base,
    resolve_ext_dir,
    resolve_token,
)


def _clean_provider_env(monkeypatch):
    for key in (
        "CEKI_PROVIDER_TOKEN",
        "PROVIDER_TOKEN",
        "CEKI_PROVIDER_EXT_DIR",
        "CEKI_EXT_DIR",
        "CEKI_API_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_resolve_token_from_arg(monkeypatch):
    _clean_provider_env(monkeypatch)
    assert resolve_token("tok-1") == "tok-1"


def test_resolve_token_from_env(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_TOKEN", "env-tok")
    assert resolve_token(None) == "env-tok"
    assert resolve_token("") == "env-tok"


def test_resolve_token_required(monkeypatch):
    _clean_provider_env(monkeypatch)
    with pytest.raises(ProviderError):
        resolve_token(None)
    with pytest.raises(ProviderError):
        resolve_token("   ")


def test_resolve_ext_dir_from_arg(tmp_path, monkeypatch):
    _clean_provider_env(monkeypatch)
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "manifest.json").write_text("{}")
    assert resolve_ext_dir(str(ext)) == str(ext)


def test_resolve_ext_dir_from_env(tmp_path, monkeypatch):
    _clean_provider_env(monkeypatch)
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "manifest.json").write_text("{}")
    monkeypatch.setenv("CEKI_PROVIDER_EXT_DIR", str(ext))
    assert resolve_ext_dir(None) == str(ext)


def test_resolve_ext_dir_missing(monkeypatch):
    _clean_provider_env(monkeypatch)
    with pytest.raises(ProviderError):
        resolve_ext_dir(None)
    with pytest.raises(ProviderError):
        resolve_ext_dir("/nonexistent/path")


def test_resolve_api_base_default(monkeypatch):
    _clean_provider_env(monkeypatch)
    assert resolve_api_base(None) == "https://api.ceki.me"


def test_resolve_api_base_env(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_API_URL", "https://api.example.test/")
    assert resolve_api_base(None) == "https://api.example.test"


def test_resolve_api_base_strips_api_suffix(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_API_URL", "https://api.example.test/api")
    assert resolve_api_base(None) == "https://api.example.test"


def test_resolve_api_base_strips_api_suffix_trailing_slash(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_API_URL", "https://api.example.test/api/")
    assert resolve_api_base(None) == "https://api.example.test"


def test_chrome_args_include_ev5421_quality_flags():
    from ceki_sdk._provider import _CHROME_ARGS
    joined = " ".join(_CHROME_ARGS)
    # audio consistency
    assert "--use-fake-ui-for-media-stream" in joined
    assert "--use-fake-device-for-media-stream" in joined
    # automation marker
    assert "--disable-blink-features=AutomationControlled" in joined
    # language/locale consistency
    assert "--lang=en-US" in joined

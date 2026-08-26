from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from ceki_sdk._provider import (
    DEFAULT_IMAGE,
    ProviderError,
    _env_map,
    _run_cmd,
    resolve_image,
    resolve_token,
    run_provider,
)


def _clean_provider_env(monkeypatch):
    for key in (
        "CEKI_PROVIDER_TOKEN",
        "PROVIDER_TOKEN",
        "CEKI_PROVIDER_IMAGE",
        "CEKI_PROVIDER_VIEWPORT",
        "CEKI_PROVIDER_LOG_LEVEL",
        "TZ",
        "DISPLAY",
        "CEKI_API_URL",
        "CEKI_WS_URL",
    ):
        monkeypatch.delenv(key, raising=False)


# ── token resolution ────────────────────────────────────────────────────────


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


# ── image resolution ────────────────────────────────────────────────────────


def test_resolve_image_default(monkeypatch):
    _clean_provider_env(monkeypatch)
    assert resolve_image(None) == DEFAULT_IMAGE


def test_resolve_image_from_env(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_IMAGE", "ceki/provider:test")
    assert resolve_image(None) == "ceki/provider:test"


def test_resolve_image_from_arg(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_IMAGE", "ceki/provider:env")
    assert resolve_image("ceki/provider:arg") == "ceki/provider:arg"


# ── container env map ───────────────────────────────────────────────────────


def test_env_map_token(monkeypatch):
    _clean_provider_env(monkeypatch)
    assert _env_map("tok") == {"CEKI_PROVIDER_TOKEN": "tok"}


def test_env_map_public_pass_through(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("TZ", "Europe/Moscow")
    monkeypatch.setenv("DISPLAY", ":1")
    env = _env_map("tok")
    assert env["TZ"] == "Europe/Moscow"
    assert env["DISPLAY"] == ":1"


def test_env_map_internal_not_forwarded(monkeypatch):
    """Internal docker-browser envs must NOT leak into the public SDK contract."""
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_API_URL", "https://api.internal.example")
    monkeypatch.setenv("CEKI_WS_URL", "wss://ws.internal.example")
    env = _env_map("tok")
    assert "CEKI_API_URL" not in env
    assert "CEKI_WS_URL" not in env


def test_env_map_viewport(monkeypatch):
    _clean_provider_env(monkeypatch)
    env = _env_map("tok", viewport="1280x720")
    assert env["CEKI_PROVIDER_VIEWPORT"] == "1280x720"


def test_env_map_verbose(monkeypatch):
    _clean_provider_env(monkeypatch)
    env = _env_map("tok", verbose=True)
    assert env["CEKI_PROVIDER_LOG_LEVEL"] == "DEBUG"


# ── docker run command ──────────────────────────────────────────────────────


def test_run_cmd_basic():
    cmd = _run_cmd("/usr/bin/docker", DEFAULT_IMAGE, {"CEKI_PROVIDER_TOKEN": "tok"})
    assert cmd == [
        "/usr/bin/docker", "run", "--rm",
        "-e", "CEKI_PROVIDER_TOKEN=tok",
        DEFAULT_IMAGE,
    ]


def test_run_cmd_timeout_appends_app_command():
    cmd = _run_cmd(
        "docker",
        DEFAULT_IMAGE,
        {"CEKI_PROVIDER_TOKEN": "tok"},
        timeout=600,
    )
    assert cmd[-4:] == [
        "python", "-m", "ceki_browser_provider.app", "--timeout=600",
    ]


# ── run_provider orchestration ─────────────────────────────────────────────


def test_docker_missing_raises(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_TOKEN", "tok")
    with patch("ceki_sdk._provider.shutil.which", return_value=None):
        with pytest.raises(ProviderError):
            run_provider()


def test_run_provider_token_required(monkeypatch):
    _clean_provider_env(monkeypatch)
    with pytest.raises(ProviderError):
        run_provider()


def test_run_provider_inspects_pulls_and_runs(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_TOKEN", "tok")
    docker_bin = "/usr/bin/docker"
    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == [docker_bin, "image", "inspect"]:
            return Mock(returncode=1)  # not present → pull next
        if cmd[:2] == [docker_bin, "pull"]:
            return Mock(returncode=0)
        raise AssertionError(f"unexpected run: {cmd}")

    def fake_call(cmd, **kwargs):
        runs.append(cmd)
        return 0

    with patch("ceki_sdk._provider.shutil.which", return_value=docker_bin), \
         patch("ceki_sdk._provider.subprocess.run", side_effect=fake_run), \
         patch("ceki_sdk._provider.subprocess.call", side_effect=fake_call):
        code = run_provider()

    assert code == 0
    assert runs == [
        [
            docker_bin, "run", "--rm",
            "-e", "CEKI_PROVIDER_TOKEN=tok",
            DEFAULT_IMAGE,
        ]
    ]


def test_run_provider_pull_failure(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_TOKEN", "tok")
    docker_bin = "/usr/bin/docker"

    def fake_run(cmd, **kwargs):
        if cmd[:3] == [docker_bin, "image", "inspect"]:
            return Mock(returncode=1)
        if cmd[:2] == [docker_bin, "pull"]:
            return Mock(returncode=1)
        raise AssertionError(f"unexpected run: {cmd}")

    with patch("ceki_sdk._provider.shutil.which", return_value=docker_bin), \
         patch("ceki_sdk._provider.subprocess.run", side_effect=fake_run):
        with pytest.raises(ProviderError):
            run_provider()


def test_run_provider_keyboard_interrupt(monkeypatch):
    _clean_provider_env(monkeypatch)
    monkeypatch.setenv("CEKI_PROVIDER_TOKEN", "tok")

    def fake_call(cmd, **kwargs):
        raise KeyboardInterrupt()

    with patch("ceki_sdk._provider.shutil.which", return_value="/usr/bin/docker"), \
         patch("ceki_sdk._provider.subprocess.run", return_value=Mock(returncode=0)), \
         patch("ceki_sdk._provider.subprocess.call", side_effect=fake_call):
        code = run_provider()
    assert code == 130

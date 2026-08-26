"""Provider mode: rent out this machine's browser through Ceki.

A thin wrapper around the official provider image. The provider itself — real
Chromium + the Ceki extension + token handshake + online poll + liveness —
lives in the public repo ``Ceki-me/docker-browser`` and ships as the Docker Hub
image ``ceki/provider``. The SDK does NOT reimplement the provider; it pulls the
image and runs it, so the launcher stays a single source of truth in
docker-browser.

CLI entry:
    ceki provider run [--token TOKEN] [--image IMAGE] [--build DIR]
                      [--viewport WxH] [--timeout SECONDS] [--verbose]

Environment variables:
    CEKI_PROVIDER_TOKEN       extension token for this browser (required)
    CEKI_PROVIDER_IMAGE       image tag (default ``ceki/provider:latest``)
    CEKI_PROVIDER_VIEWPORT    browser viewport WxH (default 1920x1080)
    CEKI_PROVIDER_LOG_LEVEL   container log verbosity (default INFO)
    TZ                        timezone passed into the container
    DISPLAY                   X display (the container starts Xvfb if unset)

Only the public provider envs are passed through — internal envs
(CEKI_WS_URL / CEKI_API_URL / update knobs) are not part of the SDK contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_IMAGE = "ceki/provider:latest"

# Public docker-browser envs forwarded from the caller's environment.
_PUBLIC_ENVS = ("CEKI_PROVIDER_VIEWPORT", "CEKI_PROVIDER_LOG_LEVEL", "TZ", "DISPLAY")

# Default command the image runs.  ``docker run`` args override the image CMD,
# so ``--timeout`` is passed by appending this command + the flag.
_APP_CMD = ("python", "-m", "ceki_browser_provider.app")


class ProviderError(Exception):
    """Raised when the provider cannot be deployed or brought online."""


def resolve_token(token: str | None = None) -> str:
    """Resolve the provider token from arg or environment."""
    value = (token or os.environ.get("CEKI_PROVIDER_TOKEN") or "").strip()
    if not value:
        raise ProviderError(
            "Provider token is required: set CEKI_PROVIDER_TOKEN or pass --token"
        )
    return value


def resolve_image(explicit: str | None = None) -> str:
    """Resolve the image tag from arg, env or the default."""
    return (explicit or os.environ.get("CEKI_PROVIDER_IMAGE") or DEFAULT_IMAGE).strip()


def _docker() -> str:
    binary = shutil.which("docker")
    if binary is None:
        raise ProviderError(
            "Docker is required to run a provider. Install Docker, or build the "
            "provider manually from https://github.com/Ceki-me/docker-browser"
        )
    return binary


def _env_map(token: str, viewport: str | None = None, verbose: bool = False) -> dict[str, str]:
    """Build the container env: token + public env pass-through + explicit args."""
    env = {"CEKI_PROVIDER_TOKEN": token}
    for name in _PUBLIC_ENVS:
        if os.environ.get(name):
            env[name] = os.environ[name]
    if viewport:
        env["CEKI_PROVIDER_VIEWPORT"] = viewport
    if verbose:
        env["CEKI_PROVIDER_LOG_LEVEL"] = "DEBUG"
    return env


def _run_cmd(
    docker: str,
    image: str,
    env: dict[str, str],
    timeout: int | None = None,
) -> list[str]:
    """Build the ``docker run`` command line (token never logged)."""
    cmd = [docker, "run", "--rm"]
    for name, value in env.items():
        cmd += ["-e", f"{name}={value}"]
    cmd.append(image)
    if timeout:
        # docker run args replace the image CMD — keep the image's default
        # entry command and append --timeout so the container self-stops.
        cmd.extend([*_APP_CMD, f"--timeout={timeout}"])
    return cmd


def _build_image(build_dir: str) -> None:
    """Build ``ceki/provider:latest`` from a local docker-browser checkout."""
    build_sh = os.path.join(build_dir, "build.sh")
    if not os.path.isfile(build_sh):
        raise ProviderError(
            f"{build_sh} not found — pass the docker-browser repo directory "
            "(https://github.com/Ceki-me/docker-browser)"
        )
    print(f"[ceki-provider] building image from {build_dir} (./build.sh) ...")
    if subprocess.call([build_sh], cwd=build_dir) != 0:
        raise ProviderError("docker-browser build.sh failed")


def run_provider(
    *,
    token: str | None = None,
    image: str | None = None,
    build: str | None = None,
    viewport: str | None = None,
    timeout: int | None = None,
    verbose: bool = False,
) -> int:
    """Pull and run the docker-browser provider image until stopped.

    Returns a process exit code (0 on clean shutdown, 130 on Ctrl-C).
    """
    token_value = resolve_token(token)
    image_value = resolve_image(image)
    docker = _docker()

    if build:
        _build_image(build)

    # Pull the public image when not present locally.
    if (
        subprocess.run([docker, "image", "inspect", image_value], capture_output=True)
        .returncode
        != 0
    ):
        print(f"[ceki-provider] pulling {image_value} ...")
        if subprocess.run([docker, "pull", image_value]).returncode != 0:
            raise ProviderError(
                f"failed to pull {image_value} — check the image name and network"
            )

    env = _env_map(token_value, viewport=viewport, verbose=verbose)
    cmd = _run_cmd(docker, image_value, env, timeout=timeout)

    print(
        f"[ceki-provider] starting {image_value} — browser online until stopped "
        "(Ctrl-C / docker stop)"
    )
    try:
        code = subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
    return 0 if code in (0, 130) else code

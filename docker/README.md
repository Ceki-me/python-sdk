# Headless browser provider (Docker)

Run a ceki **provider** browser from Docker: the container starts Chromium with
the ceki extension installed, injects your browser token and keeps the browser
online so it can be rented out as a public browser.

This is a thin wrapper around the SDK's provider mode (`ceki provider run`,
see `ceki_sdk/_provider.py`).

## Prerequisites

- Docker
- A **provider token** — create it in your account dashboard ("call a browser"
  flow). One token = one browser = one container.

## Build

The image bundles the browser-extension dist, so the build script stages it
from a local clone of the extension repo before running `docker build`:

```bash
./docker/build.sh                    # finds browser-extension/dist automatically
./docker/build.sh /path/to/dist      # ...or point at the dist explicitly
```

## Run

```bash
docker run --rm \
  -e CEKI_PROVIDER_TOKEN=<your-token> \
  ceki/provider:dev
```

The container starts an Xvfb virtual display, launches Chromium with the
extension, brings the browser **online** and keeps it there until a renter
connects or the container is stopped.

### docker compose

```bash
export CEKI_PROVIDER_TOKEN=<your-token>
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f provider
docker compose -f docker/docker-compose.yml stop provider
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `CEKI_PROVIDER_TOKEN` | — | **Required.** One-time browser token from your dashboard. |
| `CEKI_API_URL` | `https://api.ceki.me` | API base URL. |
| `CEKI_PROVIDER_EXT_DIR` | `/opt/ceki/extension` | Extension dist inside the image. |
| `DISPLAY` | `:99` | X display for the virtual screen. |

## Stopping / cleanup

`docker stop` sends SIGTERM which the provider handles gracefully: the rented
browser is closed and the browser goes offline. `docker compose stop` does the
same.

## Notes

- One browser per container. To run several providers, start several containers,
  each with its own token.
- The token is bound to the specific browser it was issued for; it cannot be
  reused for another browser.

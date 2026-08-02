# syntax=docker/dockerfile:1
#
# Ceki headless-browser provider image.
#
# Thin wrapper around the python-sdk "provider" mode:
#   * Chromium (Playwright) — the rented public browser
#   * the ceki browser extension (dist) — the provider agent inside the browser
#   * `ceki provider run` — launches Chromium + extension, injects the token and
#     keeps the browser online until a renter connects or the process stops
#
# The browser token is passed at runtime via CEKI_PROVIDER_TOKEN.
#
# Build context = python-sdk repo root. `docker/build.sh` stages the extension
# dist into docker/extension/ before `docker build`.

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

WORKDIR /opt/ceki

# Chromium runtime libraries + Xvfb virtual display (Chromium needs a display to
# run as a "visible" provider browser; Xvfb provides it headlessly).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
        libglib2.0-0 libgdk-pixbuf-2.0-0 xvfb xauth x11-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Python SDK (provider extra pulls in playwright).
COPY pyproject.toml README.md ./
COPY ceki_sdk/ ./ceki_sdk/
RUN pip install --no-cache-dir '.[provider]'

# Chromium pinned by Playwright (used only by the provider browser).
RUN python -m playwright install chromium

# Bundled browser extension dist (staged into docker/extension by build.sh).
COPY docker/extension/ /opt/ceki/extension/

# CEKI_PROVIDER_TOKEN is supplied at runtime (docker run -e ...).
ENV CEKI_PROVIDER_EXT_DIR=/opt/ceki/extension \
    CEKI_PROVIDER_LOG_LEVEL=INFO

# Entrypoint starts Xvfb (if needed) and then execs the command as PID 1 so a
# `docker stop` (SIGTERM to PID 1) reaches the provider for a clean shutdown.
COPY docker/entrypoint.sh /usr/local/bin/ceki-entrypoint
RUN chmod +x /usr/local/bin/ceki-entrypoint

ENTRYPOINT ["/usr/local/bin/ceki-entrypoint"]
CMD ["ceki", "provider", "run"]

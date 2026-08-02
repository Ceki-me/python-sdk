#!/bin/bash
# Build the ceki headless-browser provider image.
#
# Run from the python-sdk repo root (or pass the repo root as $1):
#   ./docker/build.sh
#
# What it does:
#   1. Stages the browser-extension dist into docker/extension/
#      (docker/extension/ is git-ignored; the dist never lands in git).
#   2. Runs `docker build` with the python-sdk repo as context.
#
# The extension dist is taken from the sibling clone of browser-extension:
#   $CEKI_EXT_DIST   (default: ../../browser-extension/dist)
#
# Optionally:  ./docker/build.sh /path/to/browser-extension/dist

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CEKI_IMAGE:-ceki/provider:dev}"
EXT_SRC="${1:-${CEKI_EXT_DIST:-}}"

if [ -z "${EXT_SRC:-}" ]; then
  # Try a couple of conventional locations for the extension clone.
  for cand in "$ROOT/../browser-extension/dist" "$HOME/browser-extension/dist"; do
    if [ -f "$cand/manifest.json" ]; then
      EXT_SRC="$cand"
      break
    fi
  done
fi

if [ -z "${EXT_SRC:-}" ] || [ ! -f "$EXT_SRC/manifest.json" ]; then
  echo "error: extension dist not found." >&2
  echo "  pass the dist dir explicitly:  $0 /path/to/browser-extension/dist" >&2
  echo "  or set CEKI_EXT_DIST." >&2
  exit 1
fi

echo "[ceki-provider] staging extension dist: $EXT_SRC -> docker/extension/"
rm -rf "$ROOT/docker/extension"
mkdir -p "$ROOT/docker/extension"
cp -a "$EXT_SRC"/. "$ROOT/docker/extension/"

echo "[ceki-provider] building image: $IMAGE"
docker build -t "$IMAGE" -f "$ROOT/docker/Dockerfile" "$ROOT"

echo "[ceki-provider] done: $IMAGE"
echo "  run:  docker run --rm -e CEKI_PROVIDER_TOKEN=<token> $IMAGE"

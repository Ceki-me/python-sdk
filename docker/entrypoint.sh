#!/bin/sh
# Ceki headless-provider container entrypoint.
#
# Starts a virtual display (Xvfb) if none is already running, then execs the
# real command. `exec` is important: the provider process replaces this script
# and becomes PID 1, so `docker stop` (SIGTERM to PID 1) reaches the provider
# directly and the browser session is shut down cleanly (rented browser goes
# offline, no orphaned processes).
#
# DISPLAY can be overridden by the user (e.g. to attach to an external X
# server / x11vnc).

set -e

if [ -z "${DISPLAY:-}" ]; then
  export DISPLAY=:99
fi

# Start Xvfb if it is not already up on our display.
if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
  echo "[ceki-provider] starting Xvfb on ${DISPLAY}"
  Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
fi

exec "$@"

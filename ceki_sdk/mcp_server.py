"""MCP (Model Context Protocol) stdio server for ceki-sdk.

Exposes browser.ceki.me rentals as MCP tools so any MCP client (Claude
Code, Cline, Cursor) can drive a real browser without writing CDP code.
Each tool is a thin wrapper over the existing SDK methods — no new browser
logic lives here.

Run with: ``ceki mcp`` (env: CEKI_API_KEY / CEKI_TOKEN).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from ._client import Client
from ._connect import ConnectOptions
from ._config import default_api_url, default_chat_url, default_relay_url

# ──────────────────────────────────────────────────────────────────────────
# Lazy session state — exactly ONE active browser per MCP server process.
# browser_rent creates it, browser_release closes it. The WS client is
# connected lazily on first rent (search is plain HTTP and needs no relay).
# ──────────────────────────────────────────────────────────────────────────

_client: Client | None = None
_client_error: Exception | None = None
_browser: Any | None = None


def _resolve_api_key() -> str:
    key = os.environ.get("CEKI_API_KEY") or os.environ.get("CEKI_TOKEN")
    if not key:
        raise RuntimeError(
            "CEKI_API_KEY (or CEKI_TOKEN) not set — MCP server cannot authenticate"
        )
    return key


def _connect_options() -> ConnectOptions:
    opts = ConnectOptions(reconnect=False)
    if os.environ.get("CEKI_API_URL"):
        opts.api_url = os.environ["CEKI_API_URL"]
    if os.environ.get("CEKI_RELAY_URL"):
        opts.relay_url = os.environ["CEKI_RELAY_URL"]
    if os.environ.get("CEKI_CHAT_URL"):
        opts.chat_url = os.environ["CEKI_CHAT_URL"]
    ba_user = os.environ.get("CEKI_BASIC_AUTH_USER")
    ba_pass = os.environ.get("CEKI_BASIC_AUTH_PASS")
    if ba_user and ba_pass:
        opts.basic_auth = (ba_user, ba_pass)
    return opts


async def _ensure_client() -> Client:
    """Lazy relay connect — only browser_rent needs the WS; search is HTTP.

    Builds the Client directly (NOT via :func:`ceki_sdk._connect.connect`,
    which installs its own SIGINT/SIGTERM close handlers that would fight the
    stdio server's shutdown — see :func:`main`).
    """
    global _client, _client_error
    if _client is not None:
        return _client
    if _client_error is not None:
        raise _client_error
    try:
        opts = _connect_options()
        _client = Client(
            api_key=_resolve_api_key(),
            relay_url=opts.relay_url or default_relay_url(),
            api_url=opts.api_url or default_api_url(),
            chat_url=opts.chat_url or default_chat_url(),
            reconnect=False,
            basic_auth=opts.basic_auth,
        )
        await _client._connect()
        return _client
    except Exception as exc:  # cache the failure so we don't retry on every call
        _client_error = exc
        raise


def _require_browser() -> Any:
    if _browser is None:
        raise RuntimeError("no active session — call browser_rent first")
    return _browser


# ──────────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────────


def build_server() -> FastMCP:
    """Construct the FastMCP server with the 11 Ceki browser tools."""
    mcp = FastMCP(
        "ceki",
        instructions=(
            "Rent and drive real browsers from browser.ceki.me. Call browser_rent "
            "first, use the other browser_* tools to automate, then browser_release "
            "when done."
        ),
    )

    @mcp.tool()
    async def browser_rent(
        scheduleId: int,
        mode: str = "incognito",
        maskingMode: bool = True,
    ) -> dict[str, Any]:
        """Rent a real browser by schedule_id (find one via browser_search).

        Starts an active session — exactly one at a time. Call browser_release
        before renting again.
        """
        global _browser
        if _browser is not None:
            raise RuntimeError("session already active, call browser_release first")
        client = await _ensure_client()
        browser = await client.rent(
            scheduleId,
            mode=mode,
            masking_mode=maskingMode,
        )
        _browser = browser
        return {"ok": True, "session_id": browser.session_id}

    @mcp.tool()
    async def browser_search(
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List browsers available for rent, optionally filtered by
        geo/language (filters applied server-side, e.g. {"geo": "US",
        "language": "en"})."""
        client = await _ensure_client()
        rows = await client.search(filters=filters, limit=limit)
        return [r.model_dump(mode="json") for r in rows]

    @mcp.tool()
    async def browser_navigate(
        url: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Navigate the active browser to a URL."""
        result = await _require_browser().navigate(url, timeout=timeout)
        return dict(result or {})

    @mcp.tool()
    async def browser_click(x: int, y: int) -> dict[str, Any]:
        """Click at (x, y) page coordinates in the active browser."""
        await _require_browser().click(x, y)
        return {"ok": True}

    @mcp.tool()
    async def browser_type(
        text: str,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Type text into the active browser (focused element by default, or
        a CSS selector)."""
        await _require_browser().type(text, selector=selector)
        return {"ok": True}

    @mcp.tool()
    async def browser_scroll(
        x: int = 0,
        y: int = 0,
        deltaX: int = 0,
        deltaY: int = -300,
    ) -> dict[str, Any]:
        """Scroll the active browser. deltaY negative scrolls down."""
        await _require_browser().scroll(x, y, delta_x=deltaX, delta_y=deltaY)
        return {"ok": True}

    @mcp.tool()
    async def browser_screenshot(fullPage: bool = False) -> dict[str, Any]:
        """Take a screenshot of the active browser. Returns base64 PNG image
        data — the client should decode it to an image."""
        shot = await _require_browser().screenshot(format="base64", full_page=fullPage)
        data = shot["data"] if isinstance(shot, dict) else ""
        return {"data": data, "format": "png", "full_page": fullPage}

    @mcp.tool()
    async def browser_snapshot() -> dict[str, Any]:
        """Take a screenshot plus the latest chat messages from the active
        browser."""
        snap = await _require_browser().snapshot()
        return {
            "screenshot": snap.screenshot,
            "chat": [m.model_dump(mode="json") for m in snap.chat],
            "ts": snap.ts.isoformat(),
        }

    @mcp.tool()
    async def browser_cdp(
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Escape hatch: send a raw Chrome DevTools Protocol command to the
        active browser, e.g. {"method": "DOM.getDocument"}."""
        result = await _require_browser().send(
            {"method": method, "params": params or {}},
            timeout=timeout,
        )
        return dict(result or {})

    @mcp.tool()
    async def browser_captcha(
        acceptanceTimeout: int = 60,
        completionTimeout: int = 120,
        autoAccept: bool = True,
    ) -> dict[str, Any]:
        """Request the human provider solve a captcha in the active browser
        via chat. Returns the solve result; acceptance/completion timeouts in
        seconds."""
        result = await _require_browser().request_captcha(
            acceptance_timeout=acceptanceTimeout,
            completion_timeout=completionTimeout,
            auto_accept=autoAccept,
        )
        return {
            "solved": result.solved,
            "proof_message_id": result.proof_message_id,
            "cancel_reason": result.cancel_reason,
            "child_event_id": result.child_event_id,
        }

    @mcp.tool()
    async def browser_release() -> dict[str, Any]:
        """End the active rent session and free the browser. Always call when
        done — an unreleased session keeps burning balance."""
        global _browser
        browser = _require_browser()
        _browser = None
        await browser.release()
        return {"ok": True}

    return mcp


async def _shutdown() -> None:
    """Release the active session + close the client, then exit.

    An unreleased rent keeps burning balance (project_session_lifecycle:
    WS disconnect does NOT finish the session — an explicit release is
    required), so SIGINT/SIGTERM must close it before dying.
    """
    global _browser
    try:
        if _browser is not None:
            browser = _browser
            _browser = None
            await browser.release()
        if _client is not None:
            await _client.close()
    except Exception:
        pass
    finally:
        os._exit(0)


async def _serve() -> None:
    """Build the server and run stdio under a loop we control (so the
    SIGINT/SIGTERM shutdown handlers actually run)."""
    mcp = build_server()
    loop = asyncio.get_running_loop()
    import signal as _signal

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(_shutdown()),
            )
        except NotImplementedError:
            pass  # Windows — no loop signal handlers
    await mcp.run_stdio_async()


def main() -> None:
    """Entry point for ``ceki mcp`` — runs the stdio server."""
    # Fail fast if no key — clearer than a confusing auth error mid-session.
    _resolve_api_key()
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

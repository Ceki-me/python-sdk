#!/usr/bin/env python3
"""Integration smoke tests for ceki-sdk SDK 2.2.0+.

Scenarios A–K exercising the full lifecycle through the public API.

Usage:
    python examples/smoke/mvp_smoke_v2.py --scenario A
    python examples/smoke/mvp_smoke_v2.py --scenario all
    python examples/smoke/mvp_smoke_v2.py --scenario A,I,J

Environment variables:
    CEKI_TOKEN          Sanctum token (e.g. 385|xxx)
    CEKI_API_URL        REST API base (default: https://clawapi.ittribe.org)
    CEKI_RELAY_URL      WS relay (default: wss://browser.ittribe.org/ws/agent)
    SCHEDULE_ID         Provider schedule id (default: 240)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
import traceback
from typing import Any

# Ensure the SDK is importable when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ceki_sdk import (
    CekiError,
    Client,
    ConnectOptions,
    InsufficientFunds,
    ProviderDisconnected,
    SessionEnded,
    connect,
)
from ceki_sdk._exceptions import ProviderOffline

# ── Config ──────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("CEKI_TOKEN", "")
API_URL = os.environ.get("CEKI_API_URL", "https://clawapi.ittribe.org")
RELAY_URL = os.environ.get("CEKI_RELAY_URL", "wss://browser.ittribe.org/ws/agent")
SCHEDULE_ID = int(os.environ.get("SCHEDULE_ID", "240"))

# ── Step reporting ──────────────────────────────────────────────────────────

_steps: list[tuple[str, bool, str]] = []


def step_ok(name: str, detail: str = "") -> None:
    _steps.append((name, True, detail))
    tag = f"  [{name}]"
    print(f"\033[32m  PASS {tag}\033[0m {detail}")


def step_fail(name: str, detail: str = "") -> None:
    _steps.append((name, False, detail))
    tag = f"  [{name}]"
    print(f"\033[31m  FAIL {tag}\033[0m {detail}")


def summary() -> int:
    total = len(_steps)
    passed = sum(1 for _, ok, _ in _steps if ok)
    failed = total - passed
    print()
    if failed == 0:
        print(f"\033[32m  STATUS: PASS ({passed}/{total} steps)\033[0m")
    else:
        print(f"\033[31m  STATUS: FAIL ({failed} failed, {passed} passed / {total})\033[0m")
    return 0 if failed == 0 else 1


def reset_steps() -> None:
    _steps.clear()


# ── Helpers ─────────────────────────────────────────────────────────────────

async def make_client() -> Client:
    if not TOKEN:
        raise RuntimeError("CEKI_TOKEN not set")
    return await connect(TOKEN, ConnectOptions(
        api_url=API_URL,
        relay_url=RELAY_URL,
    ))


async def wait_event(browser: Any, method: str, timeout: float = 15.0) -> dict:
    """Wait for a specific CDP event, returns its params."""
    fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    async def handler(m: str, params: dict) -> None:
        if m == method and not fut.done():
            fut.set_result(params)

    browser.on_event(handler)
    return await asyncio.wait_for(fut, timeout=timeout)


# ── Scenario A: Happy path ─────────────────────────────────────────────────

async def scenario_a() -> int:
    print("\n=== Scenario A: Happy path ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect", f"relay={RELAY_URL}")

        browser = await client.rent(SCHEDULE_ID)
        step_ok("rent", f"session={browser.session_id}")

        # First navigate creates the incognito window on the provider side;
        # CDP domains like Page.enable only work after the window exists.
        await browser.send({"method": "Page.navigate", "params": {"url": "about:blank"}})
        step_ok("window_init", "Page.navigate about:blank (creates window)")

        await browser.send({"method": "Page.enable"})
        step_ok("Page.enable")

        load_fut = asyncio.ensure_future(wait_event(browser, "Page.loadEventFired", timeout=20))
        await browser.send({"method": "Page.navigate", "params": {"url": "https://github.com"}})
        step_ok("Page.navigate", "url=https://github.com")

        await load_fut
        step_ok("Page.loadEventFired")

        title_resp = await browser.send({
            "method": "Runtime.evaluate",
            "params": {"expression": "document.title"},
        })
        title = title_resp.get("result", {}).get("value", "")
        if "GitHub" in title or "github" in title.lower():
            step_ok("title_check", f"title={title!r}")
        else:
            step_fail("title_check", f"expected 'GitHub' in title, got {title!r}")

        screenshot_resp = await browser.send({"method": "Page.captureScreenshot"})
        data = screenshot_resp.get("data", "")
        img_bytes = len(base64.b64decode(data)) if data else 0
        if img_bytes > 10_000:
            step_ok("screenshot", f"{img_bytes} bytes")
        else:
            step_fail("screenshot", f"too small: {img_bytes} bytes")

        await browser.close()
        step_ok("close")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario B: Auto-accept (same as A) ────────────────────────────────────

async def scenario_b() -> int:
    print("\n=== Scenario B: Auto-accept (same as A, requires provider auto-accept) ===")
    return await scenario_a()


# ── Scenario C: Decline offer (manual) ─────────────────────────────────────

async def scenario_c() -> int:
    print("\n=== Scenario C: Decline offer (MANUAL — decline in provider plugin) ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        print("  >>> Decline the offer in the provider plugin within 30s <<<")
        try:
            await client.rent(SCHEDULE_ID)
            step_fail("rent", "expected exception on decline, got Browser")
        except CekiError as exc:
            step_ok("rent_declined", f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario D: Offer timeout / offline provider ───────────────────────────

async def scenario_d() -> int:
    print("\n=== Scenario D: Offer timeout (offline/nonexistent provider) ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        fake_schedule = 999999
        try:
            await client.rent(fake_schedule)
            step_fail("rent", "expected error for nonexistent schedule, got Browser")
        except (ProviderOffline, ProviderDisconnected, CekiError) as exc:
            step_ok("rent_error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario E: Chrome crash (manual) ──────────────────────────────────────

async def scenario_e() -> int:
    print("\n=== Scenario E: Chrome crash (MANUAL — kill Chrome on provider side) ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        browser = await client.rent(SCHEDULE_ID)
        step_ok("rent", f"session={browser.session_id}")
        await browser.send({"method": "Page.navigate", "params": {"url": "https://example.com"}})
        await browser.send({"method": "Page.enable"})
        step_ok("navigate")
        print("  >>> Kill Chrome on provider side NOW, then wait <<<")
        try:
            reason = await browser.wait_until_ended()
            step_ok("session_ended", f"reason={reason}")
        except ProviderDisconnected:
            step_ok("provider_disconnected")
        except SessionEnded as exc:
            step_ok("session_ended", f"reason={exc.reason}")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario F: Network drop (manual) ─────────────────────────────────────

async def scenario_f() -> int:
    print("\n=== Scenario F: Network drop (MANUAL — disconnect provider for 30s) ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        browser = await client.rent(SCHEDULE_ID)
        step_ok("rent", f"session={browser.session_id}")
        await browser.send({"method": "Page.navigate", "params": {"url": "https://example.com"}})
        await browser.send({"method": "Page.enable"})
        step_ok("navigate")

        disconnected_at: float | None = None
        reconnected_at: float | None = None

        async def on_disconnect() -> None:
            nonlocal disconnected_at
            disconnected_at = time.monotonic()
            print(f"  >> provider disconnected at t={disconnected_at:.1f}")

        async def on_reconnect() -> None:
            nonlocal reconnected_at
            reconnected_at = time.monotonic()
            print(f"  >> provider reconnected at t={reconnected_at:.1f}")

        browser.on_provider_disconnected(on_disconnect)
        browser.on_provider_reconnected(on_reconnect)

        print("  >>> Disconnect provider network for ~30s, then reconnect <<<")
        try:
            reason = await asyncio.wait_for(browser.wait_until_ended(), timeout=90)
            if reconnected_at and disconnected_at:
                gap = reconnected_at - disconnected_at
                step_ok("recovery", f"disconnected {gap:.1f}s, then ended reason={reason}")
            else:
                step_ok("session_ended", f"reason={reason}")
        except asyncio.TimeoutError:
            if reconnected_at:
                step_ok("recovery", "session survived disconnect")
                await browser.close()
            else:
                step_fail("timeout", "no events in 90s")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario G: Kill session button (manual) ──────────────────────────────

async def scenario_g() -> int:
    print("\n=== Scenario G: Kill session (MANUAL — press kill in provider plugin) ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        browser = await client.rent(SCHEDULE_ID)
        step_ok("rent", f"session={browser.session_id}")
        await browser.send({"method": "Page.navigate", "params": {"url": "https://example.com"}})
        await browser.send({"method": "Page.enable"})
        step_ok("navigate")
        print("  >>> Press Kill/Stop in provider plugin NOW <<<")
        try:
            reason = await browser.wait_until_ended()
            step_ok("session_ended", f"reason={reason}")
        except SessionEnded as exc:
            step_ok("session_ended", f"reason={exc.reason}")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario H: Insufficient funds ─────────────────────────────────────────

async def scenario_h() -> int:
    print("\n=== Scenario H: Insufficient funds ===")
    no_funds_token = os.environ.get("CEKI_TOKEN_NO_FUNDS", "")
    if not no_funds_token:
        print("  SKIP: CEKI_TOKEN_NO_FUNDS not set")
        print("  To test: create a zero-balance user and set CEKI_TOKEN_NO_FUNDS")
        return 0
    reset_steps()
    client = await connect(no_funds_token, ConnectOptions(
        api_url=API_URL, relay_url=RELAY_URL,
    ))
    try:
        step_ok("connect")
        try:
            await client.rent(SCHEDULE_ID)
            step_fail("rent", "expected InsufficientFunds, got Browser")
        except InsufficientFunds as exc:
            step_ok("insufficient_funds", str(exc))
        except CekiError as exc:
            step_ok("error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario I: 10 sequential CDP commands ──────────────────────────────────

async def scenario_i() -> int:
    print("\n=== Scenario I: 10 sequential CDP commands ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        browser = await client.rent(SCHEDULE_ID)
        step_ok("rent", f"session={browser.session_id}")

        await browser.send({"method": "Page.navigate", "params": {"url": "about:blank"}})
        step_ok("window_init")
        for i in range(10):
            resp = await browser.send({
                "method": "Runtime.evaluate",
                "params": {"expression": f"1 + {i}"},
            })
            val = resp.get("result", {}).get("value")
            if val == 1 + i:
                step_ok(f"cmd_{i+1}", f"1+{i}={val}")
            else:
                step_fail(f"cmd_{i+1}", f"expected {1+i}, got {val}")

        await browser.close()
        step_ok("close")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario J: Long-running navigation ────────────────────────────────────

async def scenario_j() -> int:
    print("\n=== Scenario J: Long-running navigation (httpbin delay) ===")
    reset_steps()
    client = await make_client()
    try:
        step_ok("connect")
        browser = await client.rent(SCHEDULE_ID)
        step_ok("rent", f"session={browser.session_id}")

        await browser.send({"method": "Page.navigate", "params": {"url": "about:blank"}})
        step_ok("window_init")

        await browser.send({"method": "Page.enable"})
        step_ok("Page.enable")

        load_fut = asyncio.ensure_future(wait_event(browser, "Page.loadEventFired", timeout=30))
        t0 = time.monotonic()
        await browser.send({"method": "Page.navigate", "params": {"url": "https://httpbin.org/delay/5"}})
        step_ok("Page.navigate", "url=httpbin.org/delay/5")

        await load_fut
        elapsed = time.monotonic() - t0
        if elapsed < 30:
            step_ok("loadEventFired", f"elapsed={elapsed:.1f}s")
        else:
            step_fail("loadEventFired", f"too slow: {elapsed:.1f}s")

        await browser.close()
        step_ok("close")
    except Exception as exc:
        step_fail("exception", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        await client.close()
    return summary()


# ── Scenario registry ──────────────────────────────────────────────────────

AUTOMATIC = {"A": scenario_a, "B": scenario_b, "D": scenario_d, "H": scenario_h, "I": scenario_i, "J": scenario_j}
MANUAL = {"C": scenario_c, "E": scenario_e, "F": scenario_f, "G": scenario_g}
ALL_SCENARIOS = {**AUTOMATIC, **MANUAL}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ceki Browser SDK 2.2.0 integration smoke tests")
    parser.add_argument("--scenario", default="A", help="Scenario letter(s): A, B, C-G, H-K, all, or comma-separated (e.g. A,I,J)")
    args = parser.parse_args()

    requested = args.scenario.strip()
    if requested.lower() == "all":
        scenarios = list(AUTOMATIC.keys())
        skipped_manual = list(MANUAL.keys())
    elif requested.lower() == "k":
        print("\n=== Scenario K: Obsolete ===")
        print("  SKIP: mode parameter removed from public API in SDK 2.0+")
        return 0
    else:
        scenarios = [s.strip().upper() for s in requested.split(",")]
        skipped_manual = []

    exit_code = 0
    for key in scenarios:
        fn = ALL_SCENARIOS.get(key)
        if fn is None:
            print(f"\n  Unknown scenario: {key}")
            exit_code = 1
            continue
        if key in MANUAL and requested.lower() == "all":
            continue
        rc = asyncio.run(fn())
        if rc != 0:
            exit_code = 1

    if skipped_manual:
        print(f"\n  Skipped manual scenarios: {', '.join(skipped_manual)} (run individually)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""E2E test: fingerprint persistence across two sequential rents.

Requires a live relay + provider on browser.ittribe.org.
Run manually: python3 -m pytest tests/e2e/test_fingerprint_persistence.py -v -s

Env vars required:
  CEKI_API_KEY    — agent token (Skill Rent Agent or equivalent)
  CEKI_RELAY_URL  — wss://browser.ittribe.org/ws/agent  (default)
  CEKI_API_URL    — https://clawapi.ittribe.org          (default)
  CEKI_CHAT_URL   — https://chat.ittribe.org/api/chat    (default)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CEKI_API_KEY"),
    reason="CEKI_API_KEY not set — skip E2E (requires live relay + provider)",
)


def _opts():
    from ceki_browser import ConnectOptions
    return ConnectOptions(
        relay_url=os.environ.get("CEKI_RELAY_URL", "wss://browser.ittribe.org/ws/agent"),
        api_url=os.environ.get("CEKI_API_URL", "https://clawapi.ittribe.org"),
        chat_url=os.environ.get("CEKI_CHAT_URL", "https://chat.ittribe.org/api/chat"),
        reconnect=False,
    )


async def _eval_string(browser, expr: str) -> str:
    resp = await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True},
    })
    return resp.get("result", {}).get("value", "")


async def _eval_int(browser, expr: str) -> int:
    resp = await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True},
    })
    return int(resp.get("result", {}).get("value", 0))


async def _discover_schedule():
    from ceki_browser import connect
    api_key = os.environ["CEKI_API_KEY"]
    client = await connect(api_key, _opts())
    try:
        results = await client.search()
        if not results:
            pytest.skip("no providers online")
        return results[0].schedule_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fingerprint_persists_across_rents():
    from ceki_browser import connect

    api_key = os.environ["CEKI_API_KEY"]
    schedule_id = await _discover_schedule()

    # --- Session A: rent, collect fingerprint, export profile ---
    client_a = await connect(api_key, _opts())
    try:
        browser_a = await client_a.rent(schedule_id)
        try:
            await browser_a.navigate("about:blank")
            await asyncio.sleep(1)

            fp_a = (await browser_a.send({"method": "Browser.getFingerprint"})).get("fingerprint")
            assert fp_a is not None, "Browser.getFingerprint returned null — extension too old?"

            ua_a = await _eval_string(browser_a, "navigator.userAgent")
            tz_a = await _eval_string(browser_a, "Intl.DateTimeFormat().resolvedOptions().timeZone")
            sw_a = await _eval_int(browser_a, "screen.width")
            sh_a = await _eval_int(browser_a, "screen.height")
            hc_a = await _eval_int(browser_a, "navigator.hardwareConcurrency")
            webgl_a = await _eval_string(browser_a, """
                (() => {
                    const c = document.createElement('canvas');
                    const g = c.getContext('webgl');
                    if (!g) return 'no-webgl';
                    const e = g.getExtension('WEBGL_debug_renderer_info');
                    if (!e) return 'no-debug-ext';
                    return g.getParameter(e.UNMASKED_RENDERER_WEBGL);
                })()
            """)

            profile = await browser_a.profile.export()
        finally:
            await browser_a.close()
    finally:
        await client_a.close()

    print(f"\n--- Session A ---")
    print(f"  UA:       {ua_a}")
    print(f"  TZ:       {tz_a}")
    print(f"  Screen:   {sw_a}x{sh_a}")
    print(f"  HW conc:  {hc_a}")
    print(f"  WebGL:    {webgl_a}")
    print(f"  FP seed:  {fp_a.get('seed')}")

    # --- Session B: rent with fingerprint from profile, verify match ---
    client_b = await connect(api_key, _opts())
    try:
        browser_b = await client_b.rent(schedule_id, fingerprint=profile["fingerprint"])
        try:
            await browser_b.navigate("about:blank")
            await asyncio.sleep(1)

            fp_b = (await browser_b.send({"method": "Browser.getFingerprint"})).get("fingerprint")
            ua_b = await _eval_string(browser_b, "navigator.userAgent")
            tz_b = await _eval_string(browser_b, "Intl.DateTimeFormat().resolvedOptions().timeZone")
            sw_b = await _eval_int(browser_b, "screen.width")
            sh_b = await _eval_int(browser_b, "screen.height")
            hc_b = await _eval_int(browser_b, "navigator.hardwareConcurrency")
            webgl_b = await _eval_string(browser_b, """
                (() => {
                    const c = document.createElement('canvas');
                    const g = c.getContext('webgl');
                    if (!g) return 'no-webgl';
                    const e = g.getExtension('WEBGL_debug_renderer_info');
                    if (!e) return 'no-debug-ext';
                    return g.getParameter(e.UNMASKED_RENDERER_WEBGL);
                })()
            """)
        finally:
            await browser_b.close()
    finally:
        await client_b.close()

    print(f"\n--- Session B ---")
    print(f"  UA:       {ua_b}")
    print(f"  TZ:       {tz_b}")
    print(f"  Screen:   {sw_b}x{sh_b}")
    print(f"  HW conc:  {hc_b}")
    print(f"  WebGL:    {webgl_b}")
    print(f"  FP seed:  {fp_b.get('seed') if fp_b else 'N/A'}")

    # --- Assertions ---
    assert ua_a == ua_b, f"UA mismatch: {ua_a!r} vs {ua_b!r}"
    assert tz_a == tz_b, f"TZ mismatch: {tz_a!r} vs {tz_b!r}"
    assert sw_a == sw_b, f"screen.width mismatch: {sw_a} vs {sw_b}"
    assert sh_a == sh_b, f"screen.height mismatch: {sh_a} vs {sh_b}"
    assert hc_a == hc_b, f"hardwareConcurrency mismatch: {hc_a} vs {hc_b}"
    assert webgl_a == webgl_b, f"WebGL mismatch: {webgl_a!r} vs {webgl_b!r}"
    assert fp_a == fp_b, f"CDP fingerprint mismatch"

    print("\n✅ All fingerprint values match between Session A and Session B")

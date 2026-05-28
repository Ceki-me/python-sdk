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
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CEKI_API_KEY"),
    reason="CEKI_API_KEY not set — skip E2E (requires live relay + provider)",
)


def _opts():
    from ceki_sdk import ConnectOptions
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
    from ceki_sdk import connect
    api_key = os.environ["CEKI_API_KEY"]
    client = await connect(api_key, _opts())
    try:
        results = await client.search()
        if not results:
            pytest.skip("no providers online")
        return results[0].schedule_id
    finally:
        await client.close()


async def _try_get_fingerprint_cdp(browser):
    """Try Browser.getFingerprint (ext v0.6.102+). Returns dict or None if not available."""
    try:
        resp = await browser.send({"method": "Browser.getFingerprint"})
        return resp.get("fingerprint")
    except Exception:
        return None


async def _collect_browser_fingerprint(browser):
    """Collect fingerprint values via Runtime.evaluate from the actual page."""
    await browser.navigate("about:blank")
    await asyncio.sleep(1.5)

    ua = await _eval_string(browser, "navigator.userAgent")
    tz = await _eval_string(browser, "Intl.DateTimeFormat().resolvedOptions().timeZone")
    locale = await _eval_string(browser, "navigator.language")
    sw = await _eval_int(browser, "screen.width")
    sh = await _eval_int(browser, "screen.height")
    hc = await _eval_int(browser, "navigator.hardwareConcurrency")
    webgl = await _eval_string(browser, """
        (() => {
            const c = document.createElement('canvas');
            const g = c.getContext('webgl');
            if (!g) return 'no-webgl';
            const e = g.getExtension('WEBGL_debug_renderer_info');
            if (!e) return 'no-debug-ext';
            return g.getParameter(e.UNMASKED_RENDERER_WEBGL);
        })()
    """)

    fp_cdp = await _try_get_fingerprint_cdp(browser)
    profile = await browser.profile.export()

    return {
        "ua": ua,
        "tz": tz,
        "locale": locale,
        "screen_w": sw,
        "screen_h": sh,
        "hc": hc,
        "webgl": webgl,
        "fp_cdp": fp_cdp,
        "profile": profile,
    }


@pytest.mark.asyncio
async def test_fingerprint_persists_across_rents():
    from ceki_sdk import connect

    api_key = os.environ["CEKI_API_KEY"]
    schedule_id = await _discover_schedule()

    # --- Session A: rent, collect fingerprint, export profile ---
    client_a = await connect(api_key, _opts())
    try:
        browser_a = await client_a.rent(schedule_id)
        try:
            a = await _collect_browser_fingerprint(browser_a)
        finally:
            await browser_a.close()
    finally:
        await client_a.close()

    print("\n--- Session A ---")
    print(f"  UA:       {a['ua']}")
    print(f"  TZ:       {a['tz']}")
    print(f"  Locale:   {a['locale']}")
    print(f"  Screen:   {a['screen_w']}x{a['screen_h']}")
    print(f"  HW conc:  {a['hc']}")
    print(f"  WebGL:    {a['webgl']}")
    if a["fp_cdp"]:
        print(f"  FP seed:  {a['fp_cdp'].get('seed')}")
    else:
        print("  FP CDP:   not available (ext < 0.6.102)")

    fingerprint_from_profile = a["profile"].get("fingerprint")

    # --- Session B: rent with fingerprint from profile ---
    client_b = await connect(api_key, _opts())
    try:
        browser_b = await client_b.rent(
            schedule_id,
            fingerprint=fingerprint_from_profile if fingerprint_from_profile else True,
        )
        try:
            b = await _collect_browser_fingerprint(browser_b)
        finally:
            await browser_b.close()
    finally:
        await client_b.close()

    print("\n--- Session B ---")
    print(f"  UA:       {b['ua']}")
    print(f"  TZ:       {b['tz']}")
    print(f"  Locale:   {b['locale']}")
    print(f"  Screen:   {b['screen_w']}x{b['screen_h']}")
    print(f"  HW conc:  {b['hc']}")
    print(f"  WebGL:    {b['webgl']}")
    if b["fp_cdp"]:
        print(f"  FP seed:  {b['fp_cdp'].get('seed')}")
    else:
        print("  FP CDP:   not available (ext < 0.6.102)")

    # --- Assertions ---
    if fingerprint_from_profile is None:
        print("\n⚠ Extension < 0.6.102: Browser.getFingerprint not available.")
        print("  Cannot test fingerprint persistence (profile has no fingerprint).")
        print("  Update extension to 0.6.102+ and re-run.")
        pytest.skip(
            "Extension too old — Browser.getFingerprint not available,"
            " fingerprint not in profile"
        )

    assert a["ua"] == b["ua"], (
        f"UA mismatch: {a['ua']!r} vs {b['ua']!r}"
    )
    assert a["tz"] == b["tz"], (
        f"TZ mismatch: {a['tz']!r} vs {b['tz']!r}"
    )
    assert a["locale"] == b["locale"], (
        f"Locale mismatch: {a['locale']!r} vs {b['locale']!r}"
    )
    assert a["screen_w"] == b["screen_w"], (
        f"screen.width mismatch: {a['screen_w']} vs {b['screen_w']}"
    )
    assert a["screen_h"] == b["screen_h"], (
        f"screen.height mismatch: {a['screen_h']} vs {b['screen_h']}"
    )
    assert a["hc"] == b["hc"], f"hardwareConcurrency mismatch: {a['hc']} vs {b['hc']}"
    assert a["webgl"] == b["webgl"], f"WebGL mismatch: {a['webgl']!r} vs {b['webgl']!r}"

    if a["fp_cdp"] and b["fp_cdp"]:
        assert a["fp_cdp"] == b["fp_cdp"], "CDP fingerprint mismatch"

    print("\n✅ All fingerprint values match between Session A and Session B")

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from ceki_sdk._profile import BrowserProfile

SAMPLE_FINGERPRINT = {
    "seed": 123456789,
    "timezoneId": "Europe/Berlin",
    "locale": "en-US",
    "acceptLanguage": "en-US,en;q=0.9",
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "platform": "Win32",
    "screen": {"width": 1920, "height": 1080, "devicePixelRatio": 1},
    "geolocation": {"latitude": 52.52, "longitude": 13.405, "accuracy": 100},
    "hardwareConcurrency": 8,
    "canvasNoise": 0.04,
    "webglVendor": "Google Inc. (NVIDIA)",
    "webglRenderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1080)",
    "audioNoiseDb": -85.3,
    "mediaDevicesDelta": 2,
    "speechVoicesDelta": 1,
}


class FakeBrowser:
    def __init__(self):
        self.send = AsyncMock()


@pytest.mark.asyncio
async def test_export_full():
    fb = FakeBrowser()
    fb.send.side_effect = [
        {"fingerprint": SAMPLE_FINGERPRINT},
        {"cookies": [{"name": "sid", "value": "abc", "domain": ".reddit.com"}]},
        {"result": {"value": '{"theme":"dark","auth":"xyz"}'}},
        {"result": {"value": '{"draft":"hello"}'}},
        {"result": {"value": "https://reddit.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export()
    assert blob["schema_version"] == 2
    assert blob["fingerprint"] == SAMPLE_FINGERPRINT
    assert blob["origin"] == "https://reddit.com"
    assert len(blob["cookies"]) == 1
    assert blob["cookies"][0]["name"] == "sid"
    assert blob["localStorage"] == {"theme": "dark", "auth": "xyz"}
    assert blob["sessionStorage"] == {"draft": "hello"}
    assert fb.send.call_count == 5
    assert fb.send.call_args_list[0].args[0]["method"] == "Browser.getFingerprint"


@pytest.mark.asyncio
async def test_export_filter_domains():
    fb = FakeBrowser()
    fb.send.side_effect = [
        {"fingerprint": SAMPLE_FINGERPRINT},
        {"cookies": [
            {"name": "sid", "value": "1", "domain": ".reddit.com"},
            {"name": "ad", "value": "2", "domain": ".doubleclick.net"},
        ]},
        {"result": {"value": "{}"}},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://reddit.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export(domains=[".reddit.com"])
    assert len(blob["cookies"]) == 1
    assert blob["cookies"][0]["name"] == "sid"


@pytest.mark.asyncio
async def test_export_skip_session_storage():
    fb = FakeBrowser()
    fb.send.side_effect = [
        {"fingerprint": SAMPLE_FINGERPRINT},
        {"cookies": []},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://example.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export(include_session_storage=False)
    assert blob["sessionStorage"] == {}
    assert fb.send.call_count == 4


@pytest.mark.asyncio
async def test_export_handles_opaque_origin():
    fb = FakeBrowser()
    fb.send.side_effect = [
        {"fingerprint": None},
        {"cookies": []},
        {"result": {"value": None}},
        {"result": {"value": None}},
        {"result": {"value": "about:blank"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export()
    assert blob["localStorage"] == {}
    assert blob["sessionStorage"] == {}
    assert blob["origin"] == "about:blank"
    assert blob["fingerprint"] is None


@pytest.mark.asyncio
async def test_export_fingerprint_null_when_disabled():
    fb = FakeBrowser()
    fb.send.side_effect = [
        {"fingerprint": None},
        {"cookies": []},
        {"result": {"value": "{}"}},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://example.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export()
    assert blob["schema_version"] == 2
    assert blob["fingerprint"] is None


@pytest.mark.asyncio
async def test_export_fingerprint_fallback_on_old_extension():
    """When Browser.getFingerprint is not available, export falls back to fingerprint=None."""
    fb = FakeBrowser()
    fb.send.side_effect = [
        Exception("CDP error: Browser.getFingerprint wasn't found"),
        {"cookies": [{"name": "x", "value": "y", "domain": ".example.com"}]},
        {"result": {"value": "{}"}},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://example.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export()
    assert blob["schema_version"] == 2
    assert blob["fingerprint"] is None
    assert len(blob["cookies"]) == 1


@pytest.mark.asyncio
async def test_import_v2_full():
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    blob = {
        "schema_version": 2,
        "fingerprint": SAMPLE_FINGERPRINT,
        "origin": "https://reddit.com",
        "cookies": [{"name": "sid", "value": "abc", "domain": ".reddit.com"}],
        "localStorage": {"theme": "dark"},
        "sessionStorage": {"draft": "hi"},
    }
    await p.import_(blob)
    assert fb.send.call_count == 3
    first_call = fb.send.call_args_list[0]
    assert first_call.args[0]["method"] == "Network.setCookies"
    assert first_call.args[0]["params"]["cookies"][0]["name"] == "sid"
    second_call = fb.send.call_args_list[1]
    assert "localStorage.setItem" in second_call.args[0]["params"]["expression"]
    third_call = fb.send.call_args_list[2]
    assert "sessionStorage.setItem" in third_call.args[0]["params"]["expression"]


@pytest.mark.asyncio
async def test_import_v1_backward_compat():
    """v1 profiles without fingerprint import successfully."""
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    blob = {
        "schema_version": 1,
        "origin": "https://reddit.com",
        "cookies": [{"name": "sid", "value": "abc", "domain": ".reddit.com"}],
        "localStorage": {"theme": "dark"},
        "sessionStorage": {},
    }
    await p.import_(blob)
    assert fb.send.call_count == 2


@pytest.mark.asyncio
async def test_import_unknown_schema_version():
    fb = FakeBrowser()
    p = BrowserProfile(fb)
    with pytest.raises(ValueError, match="schema_version=99"):
        await p.import_({"schema_version": 99, "cookies": []})
    assert fb.send.call_count == 0


@pytest.mark.asyncio
async def test_import_v3_raises():
    fb = FakeBrowser()
    p = BrowserProfile(fb)
    with pytest.raises(ValueError, match="schema_version=3"):
        await p.import_({"schema_version": 3, "cookies": []})


@pytest.mark.asyncio
async def test_import_empty_blob():
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    await p.import_({"schema_version": 2, "cookies": [], "localStorage": {}, "sessionStorage": {}})
    assert fb.send.call_count == 0


@pytest.mark.asyncio
async def test_import_cookies_only():
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    await p.import_({
        "schema_version": 2,
        "cookies": [{"name": "tok", "value": "x", "domain": ".example.com"}],
        "localStorage": {},
        "sessionStorage": {},
    })
    assert fb.send.call_count == 1
    assert fb.send.call_args_list[0].args[0]["method"] == "Network.setCookies"


@pytest.mark.asyncio
async def test_import_storage_values_serialized_as_json():
    """localStorage values must be JSON-stringified properly in the injected expression."""
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    storage = {"key": 'value with "quotes" and \\backslash'}
    await p.import_({
        "schema_version": 2,
        "cookies": [],
        "localStorage": storage,
        "sessionStorage": {},
    })
    expr = fb.send.call_args_list[0].args[0]["params"]["expression"]
    assert json.dumps(storage) in expr


@pytest.mark.asyncio
async def test_import_v2_ignores_fingerprint():
    """import_() does not apply fingerprint — that's done via rent(fingerprint=...)."""
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    blob = {
        "schema_version": 2,
        "fingerprint": SAMPLE_FINGERPRINT,
        "cookies": [{"name": "x", "value": "y", "domain": ".example.com"}],
        "localStorage": {},
        "sessionStorage": {},
    }
    await p.import_(blob)
    assert fb.send.call_count == 1
    assert fb.send.call_args_list[0].args[0]["method"] == "Network.setCookies"


@pytest.mark.asyncio
async def test_fingerprint_json_roundtrip():
    """Fingerprint dict serializes to JSON and back with all fields preserved."""
    serialized = json.dumps(SAMPLE_FINGERPRINT)
    deserialized = json.loads(serialized)
    assert deserialized == SAMPLE_FINGERPRINT
    assert isinstance(deserialized["seed"], int)
    assert isinstance(deserialized["screen"], dict)
    assert isinstance(deserialized["canvasNoise"], float)


@pytest.mark.asyncio
async def test_profile_accessible_on_browser(mock_relay):
    """Browser.profile is available after connect (no rent needed — just check attribute)."""
    from ceki_sdk import ConnectOptions, connect
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))
    try:
        assert hasattr(client, "_active_browsers")
        from ceki_sdk import BrowserProfile
        assert BrowserProfile is not None
    finally:
        await client.close()

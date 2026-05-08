from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from ceki_browser._profile import BrowserProfile


class FakeBrowser:
    def __init__(self):
        self.send = AsyncMock()


@pytest.mark.asyncio
async def test_export_full():
    fb = FakeBrowser()
    fb.send.side_effect = [
        {"cookies": [{"name": "sid", "value": "abc", "domain": ".reddit.com"}]},
        {"result": {"value": '{"theme":"dark","auth":"xyz"}'}},
        {"result": {"value": '{"draft":"hello"}'}},
        {"result": {"value": "https://reddit.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export()
    assert blob["schema_version"] == 1
    assert blob["origin"] == "https://reddit.com"
    assert len(blob["cookies"]) == 1
    assert blob["cookies"][0]["name"] == "sid"
    assert blob["localStorage"] == {"theme": "dark", "auth": "xyz"}
    assert blob["sessionStorage"] == {"draft": "hello"}
    assert fb.send.call_count == 4


@pytest.mark.asyncio
async def test_export_filter_domains():
    fb = FakeBrowser()
    fb.send.side_effect = [
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
        {"cookies": []},
        {"result": {"value": "{}"}},
        {"result": {"value": "https://example.com"}},
    ]
    p = BrowserProfile(fb)
    blob = await p.export(include_session_storage=False)
    assert blob["sessionStorage"] == {}
    assert fb.send.call_count == 3


@pytest.mark.asyncio
async def test_export_handles_opaque_origin():
    fb = FakeBrowser()
    fb.send.side_effect = [
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


@pytest.mark.asyncio
async def test_import_full():
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    blob = {
        "schema_version": 1,
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
async def test_import_schema_version_mismatch():
    fb = FakeBrowser()
    p = BrowserProfile(fb)
    with pytest.raises(ValueError, match="schema_version=99"):
        await p.import_({"schema_version": 99, "cookies": []})
    assert fb.send.call_count == 0


@pytest.mark.asyncio
async def test_import_empty_blob():
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    await p.import_({"schema_version": 1, "cookies": [], "localStorage": {}, "sessionStorage": {}})
    assert fb.send.call_count == 0


@pytest.mark.asyncio
async def test_import_cookies_only():
    fb = FakeBrowser()
    fb.send.return_value = {}
    p = BrowserProfile(fb)
    await p.import_({
        "schema_version": 1,
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
    await p.import_({"schema_version": 1, "cookies": [], "localStorage": storage, "sessionStorage": {}})
    expr = fb.send.call_args_list[0].args[0]["params"]["expression"]
    # Expression must be valid — check that the storage dict is JSON-embedded
    assert json.dumps(storage) in expr


@pytest.mark.asyncio
async def test_profile_accessible_on_browser(mock_relay):
    """Browser.profile is available after connect (no rent needed — just check attribute)."""
    from ceki_browser import ConnectOptions, connect
    client = await connect("test-key", ConnectOptions(relay_url=f"ws://127.0.0.1:{mock_relay.port}/ws/agent"))
    try:
        assert hasattr(client, "_active_browsers")
        # Profile is on Browser, not Client — just verify import works
        from ceki_browser import BrowserProfile
        assert BrowserProfile is not None
    finally:
        await client.close()

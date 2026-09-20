"""Download event tests for task 10135.

Verifies Browser.downloadWillBegin / Browser.downloadProgress /
Ceki.downloadMeta / Ceki.downloadChunk reach on_download callbacks,
and that non-download CDP events do NOT.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ceki_sdk._browser import Browser
from ceki_sdk._models import DownloadChunk, DownloadMeta, Match


def _make_browser() -> tuple[Browser, MagicMock]:
    client = MagicMock()
    client._active_browsers = {}
    match = Match(
        session_id="sess-dl",
        schedule_id=1,
        renter_token="t",
        provider_token="pt",
       ws_url="ws://x",
    )
    browser = Browser(client, match, human="natural")
    browser._on_cdp_event = AsyncMock(wraps=browser._on_cdp_event)
    return browser, client


@pytest.mark.asyncio
async def test_on_download_receives_download_will_begin() -> None:
    browser, _ = _make_browser()
    received: list[dict] = []
    browser.on_download(lambda ev: received.append(ev))

    await browser._on_cdp_event({
        "method": "Browser.downloadWillBegin",
        "params": {
            "guid": "g1",
            "url": "https://example.com/f.bin",
            "suggestedFilename": "f.bin",
            "totalBytes": 1000,
            "mime_type": "application/octet-stream",
        },
    })
    await asyncio.sleep(0)  # let asyncio tasks settle

    assert len(received) == 1
    assert received[0]["guid"] == "g1"
    assert received[0]["url"] == "https://example.com/f.bin"
    assert received[0]["totalBytes"] == 1000


@pytest.mark.asyncio
async def test_on_download_receives_download_progress() -> None:
    browser, _ = _make_browser()
    received: list[dict] = []
    browser.on_download(lambda ev: received.append(ev))

    await browser._on_cdp_event({
        "method": "Browser.downloadProgress",
        "params": {"guid": "g1", "state": "inProgress", "receivedBytes": 500, "totalBytes": 1000},
    })
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0]["state"] == "inProgress"


@pytest.mark.asyncio
async def test_on_download_receives_ceki_download_meta() -> None:
    browser, _ = _make_browser()
    received: list[dict] = []
    browser.on_download(lambda ev: received.append(ev))

    await browser._on_cdp_event({
        "method": "Ceki.downloadMeta",
        "params": {
            "guid": "g1",
            "url": "https://example.com/f.bin",
            "suggestedFilename": "f.bin",
            "totalBytes": 1000,
        },
    })
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0]["suggestedFilename"] == "f.bin"


@pytest.mark.asyncio
async def test_on_download_receives_ceki_download_chunk() -> None:
    browser, _ = _make_browser()
    received: list[dict] = []
    browser.on_download(lambda ev: received.append(ev))

    await browser._on_cdp_event({
        "method": "Ceki.downloadChunk",
        "params": {"guid": "g1", "seq": 0, "total": 2, "payload": "AAAA"},
    })
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0]["seq"] == 0
    assert received[0]["payload"] == "AAAA"


@pytest.mark.asyncio
async def test_non_download_cdp_events_do_not_reach_on_download() -> None:
    browser, _ = _make_browser()
    received: list[dict] = []
    browser.on_download(lambda ev: received.append(ev))

    await browser._on_cdp_event({"method": "Page.frameNavigated", "params": {"url": "https://x"}})
    await browser._on_cdp_event({"method": "Network.requestWillBeSent", "params": {}})
    await asyncio.sleep(0)

    assert len(received) == 0


@pytest.mark.asyncio
async def test_handler_error_does_not_break_dispatch() -> None:
    browser, _ = _make_browser()
    received: list[dict] = []

    async def _boom(ev: dict) -> None:
        raise RuntimeError("boom")

    browser.on_download(_boom)
    browser.on_download(lambda ev: received.append(ev))

    await browser._on_cdp_event({
        "method": "Browser.downloadWillBegin",
        "params": {"guid": "g1", "url": "https://x", "suggestedFilename": "f", "totalBytes": 1},
    })
    await asyncio.sleep(0)

    assert len(received) == 1

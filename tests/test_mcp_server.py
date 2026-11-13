"""MCP server tests — tool registration (no live browser needed)."""
import asyncio

import pytest

from ceki_sdk.mcp_server import build_server

EXPECTED_TOOLS = {
    "browser_rent",
    "browser_search",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_screenshot",
    "browser_snapshot",
    "browser_cdp",
    "browser_captcha",
    "browser_release",
}


@pytest.mark.asyncio
async def test_registers_all_11_tools():
    mcp = build_server()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_tools_have_descriptions():
    mcp = build_server()
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in EXPECTED_TOOLS:
        assert tools[name].description, f"{name} missing description"


def test_import_main():
    from ceki_sdk.mcp_server import main  # noqa: F401
    assert callable(main)

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceki_browser._browser import Browser
from ceki_browser.cli import build_parser


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _make_browser() -> Browser:
    client = AsyncMock()
    client._active_browsers = {}
    client.chat_url = "https://test/chat"
    client.api_key = "test"

    match = AsyncMock()
    match.session_id = "upload-1"
    match.schedule_id = 1
    match.chat_topic_id = "t1"
    match.browser_info = {}
    match.provider_user_id = None

    with patch.dict("os.environ", {"CEKI_HUMAN_DISABLE": "1"}):
        return Browser(client, match)


# ──────────────────────────────────────────────────────────────────────────
# browser.upload() with file path
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_file_path(tmp_path: Path):
    b = _make_browser()
    test_file = tmp_path / "doc.pdf"
    test_file.write_bytes(b"%PDF-1.4 test content")

    b.send = AsyncMock(return_value={
        "result": {
            "value": json.dumps({"ok": True, "filename": "doc.pdf", "size": 21}),
        }
    })

    result = await b.upload("input[type=file]", test_file)
    assert result == {"ok": True, "filename": "doc.pdf", "size": 21}

    # Verify send was called with Runtime.evaluate
    call_args = b.send.call_args[0][0]
    assert call_args["method"] == "Runtime.evaluate"
    expr = call_args["params"]["expression"]
    assert "document.querySelector" in expr
    assert "doc.pdf" in expr


# ──────────────────────────────────────────────────────────────────────────
# browser.upload() with bytes + custom filename
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_bytes_custom_filename():
    b = _make_browser()
    data = b"hello world"

    b.send = AsyncMock(return_value={
        "result": {
            "value": json.dumps({"ok": True, "filename": "custom.txt", "size": 11}),
        }
    })

    result = await b.upload("#file-input", data, filename="custom.txt")
    assert result == {"ok": True, "filename": "custom.txt", "size": 11}

    call_args = b.send.call_args[0][0]
    expr = call_args["params"]["expression"]
    b64 = base64.b64encode(data).decode("ascii")
    assert b64 in expr
    assert "custom.txt" in expr


# ──────────────────────────────────────────────────────────────────────────
# browser.upload() bytes without filename defaults to upload.bin
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_bytes_default_filename():
    b = _make_browser()

    b.send = AsyncMock(return_value={
        "result": {
            "value": json.dumps({"ok": True, "filename": "upload.bin", "size": 3}),
        }
    })

    result = await b.upload("input", b"\x00\x01\x02")
    assert result["filename"] == "upload.bin"

    call_args = b.send.call_args[0][0]
    expr = call_args["params"]["expression"]
    assert "upload.bin" in expr
    assert "application/octet-stream" in expr


# ──────────────────────────────────────────────────────────────────────────
# JS expression escapes special chars in filenames
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_escapes_special_chars(tmp_path: Path):
    b = _make_browser()
    # Filename with quotes and backslash
    test_file = tmp_path / "normal.png"
    test_file.write_bytes(b"\x89PNG")

    b.send = AsyncMock(return_value={
        "result": {
            "value": json.dumps({"ok": True, "filename": 'file"with\'quotes.png', "size": 4}),
        }
    })

    result = await b.upload("input", test_file, filename='file"with\'quotes.png')

    call_args = b.send.call_args[0][0]
    expr = call_args["params"]["expression"]
    # json.dumps properly escapes the double quote
    assert r'file\"with' in expr
    # The expression should be valid JS (no syntax error from unescaped quotes)
    assert "image/png" in expr


# ──────────────────────────────────────────────────────────────────────────
# upload raises ValueError on "no input matched"
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_no_input_matched():
    b = _make_browser()

    b.send = AsyncMock(return_value={
        "result": {
            "value": json.dumps({"error": "no input matched"}),
        }
    })

    with pytest.raises(ValueError, match="no input matched"):
        await b.upload("#missing", b"data", filename="f.txt")


# ──────────────────────────────────────────────────────────────────────────
# upload raises ValueError on "not a file input"
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_not_file_input():
    b = _make_browser()

    b.send = AsyncMock(return_value={
        "result": {
            "value": json.dumps({"error": "element is not a file input"}),
        }
    })

    with pytest.raises(ValueError, match="element is not a file input"):
        await b.upload("#text-input", b"data", filename="f.txt")


# ──────────────────────────────────────────────────────────────────────────
# upload raises ValueError for missing file path
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_file_not_found():
    b = _make_browser()
    with pytest.raises(ValueError, match="file not found"):
        await b.upload("input", "/nonexistent/path/file.txt")


# ──────────────────────────────────────────────────────────────────────────
# MIME type detection
# ──────────────────────────────────────────────────────────────────────────


async def test_upload_mime_type_png(tmp_path: Path):
    b = _make_browser()
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG")

    b.send = AsyncMock(return_value={
        "result": {"value": json.dumps({"ok": True, "filename": "image.png", "size": 4})}
    })

    await b.upload("input", f)
    expr = b.send.call_args[0][0]["params"]["expression"]
    assert "image/png" in expr


async def test_upload_mime_type_pdf(tmp_path: Path):
    b = _make_browser()
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")

    b.send = AsyncMock(return_value={
        "result": {"value": json.dumps({"ok": True, "filename": "doc.pdf", "size": 8})}
    })

    await b.upload("input", f)
    expr = b.send.call_args[0][0]["params"]["expression"]
    assert "application/pdf" in expr


async def test_upload_mime_type_unknown(tmp_path: Path):
    b = _make_browser()
    f = tmp_path / "data.qzx"
    f.write_bytes(b"binary data")

    b.send = AsyncMock(return_value={
        "result": {"value": json.dumps({"ok": True, "filename": "data.qzx", "size": 11})}
    })

    await b.upload("input", f)
    expr = b.send.call_args[0][0]["params"]["expression"]
    assert "application/octet-stream" in expr


# ──────────────────────────────────────────────────────────────────────────
# CLI parser test
# ──────────────────────────────────────────────────────────────────────────


def test_parser_upload():
    parser = build_parser()
    args = parser.parse_args([
        "upload", "ses-1", "--selector", "input[type=file]",
        "--file", "/tmp/doc.pdf",
    ])
    assert args.command == "upload"
    assert args.session_id == "ses-1"
    assert args.selector == "input[type=file]"
    assert args.file_path == "/tmp/doc.pdf"
    assert args.filename is None


def test_parser_upload_with_filename():
    parser = build_parser()
    args = parser.parse_args([
        "upload", "ses-1", "--selector", "#upload",
        "--file", "/tmp/doc.pdf", "--filename", "renamed.pdf",
    ])
    assert args.filename == "renamed.pdf"


# ──────────────────────────────────────────────────────────────────────────
# CLI upload with missing file → exit 1 + error JSON
# ──────────────────────────────────────────────────────────────────────────


def test_cli_upload_missing_file():
    env = {**__import__("os").environ, "CEKI_API_KEY": "test-key"}
    result = subprocess.run(
        [
            sys.executable, "-m", "ceki_browser.cli",
            "upload", "ses-1",
            "--selector", "input[type=file]",
            "--file", "/nonexistent/path/file.txt",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1
    err = json.loads(result.stderr.strip())
    assert "file not found" in err["error"]

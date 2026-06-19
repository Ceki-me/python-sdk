from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from ceki_sdk.cli import build_parser
from ceki_sdk.contract import ContractClient, ContractError
from ceki_sdk.timelog import TimelogClient

# ── helpers ───────────────────────────────────────────────────────


def _http_mock(payload, status: int = 200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = resp
    return client, resp


def _mcp_text(obj) -> dict:
    return {"result": {"content": [{"type": "text", "text": json.dumps(obj)}]}}


def _captured_body(http: MagicMock) -> dict:
    return http.post.call_args.kwargs["json"]


def _timelog(http: MagicMock) -> TimelogClient:
    cc = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    return TimelogClient(contract=cc)


# ── tool name mapping ─────────────────────────────────────────────


def test_start_calls_timelog_start():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    tl = _timelog(http)
    tl.start(42)
    body = _captured_body(http)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "timelog-start"
    assert body["params"]["arguments"] == {"event_id": 42}


def test_stop_without_label_omits_field():
    http, _ = _http_mock(_mcp_text({"id": 2}))
    tl = _timelog(http)
    tl.stop(42)
    body = _captured_body(http)
    assert body["params"]["name"] == "timelog-stop"
    assert body["params"]["arguments"] == {"event_id": 42}
    assert "label" not in body["params"]["arguments"]


def test_stop_with_label():
    http, _ = _http_mock(_mcp_text({"id": 3}))
    tl = _timelog(http)
    tl.stop(42, label="что сделал")
    body = _captured_body(http)
    assert body["params"]["name"] == "timelog-stop"
    assert body["params"]["arguments"] == {"event_id": 42, "label": "что сделал"}


def test_check_calls_timelog_check():
    http, _ = _http_mock(_mcp_text({"open": False}))
    tl = _timelog(http)
    assert tl.check(7) == {"open": False}
    body = _captured_body(http)
    assert body["params"]["name"] == "timelog-check"
    assert body["params"]["arguments"] == {"event_id": 7}


def test_start_unwraps_text_json():
    http, _ = _http_mock(_mcp_text({"started_at": "2026-06-17T10:00:00Z"}))
    tl = _timelog(http)
    assert tl.start(1) == {"started_at": "2026-06-17T10:00:00Z"}


def test_error_propagates_as_contract_error():
    http, _ = _http_mock({"error": {"code": -32000, "message": "no open log"}})
    tl = _timelog(http)
    with pytest.raises(ContractError):
        tl.stop(1)


def test_event_id_coerced_to_int():
    http, _ = _http_mock(_mcp_text({}))
    tl = _timelog(http)
    tl.start("42")  # str passed through int()
    args = _captured_body(http)["params"]["arguments"]
    assert args == {"event_id": 42}


# ── CLI parser ────────────────────────────────────────────────────


def test_parser_timelog_start():
    a = build_parser().parse_args(["timelog", "start", "42"])
    assert a.command == "timelog"
    assert a.timelog_action == "start"
    assert a.event_id == 42


def test_parser_timelog_stop_no_label():
    a = build_parser().parse_args(["timelog", "stop", "42"])
    assert a.timelog_action == "stop"
    assert a.event_id == 42
    assert a.label is None


def test_parser_timelog_stop_with_label():
    a = build_parser().parse_args(["timelog", "stop", "42", "--label", "fixed bug"])
    assert a.timelog_action == "stop"
    assert a.label == "fixed bug"


def test_parser_timelog_check():
    a = build_parser().parse_args(["timelog", "check", "42"])
    assert a.timelog_action == "check"
    assert a.event_id == 42


def test_parser_timelog_is_top_level_not_under_contract():
    # ensure ceki contract timelog-start does NOT exist as a subcommand
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["contract", "timelog-start", "42"])


def test_parser_timelog_event_id_required():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["timelog", "start"])

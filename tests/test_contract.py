from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from ceki_sdk.cli import build_parser
from ceki_sdk.contract import (
    ContractClient,
    ContractError,
    _benefitable,
    _clean,
    contract_ids_from_env,
)

# ── helpers ───────────────────────────────────────────────────────


def _http_mock(payload, status: int = 200):
    """Build a MagicMock httpx.Client whose .post/.get return given payload."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = resp
    client.get.return_value = resp
    return client, resp


def _mcp_text(obj) -> dict:
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(obj)}]
        }
    }


# ── _benefitable / _clean ─────────────────────────────────────────


def test_benefitable_agent():
    assert _benefitable("agent:8") == {"type": "agent", "value": 8}


def test_benefitable_user():
    assert _benefitable("user:61") == {"type": "user", "value": 61}


def test_benefitable_none():
    assert _benefitable(None) is None
    assert _benefitable("") is None


def test_benefitable_malformed_raises():
    with pytest.raises(ValueError):
        _benefitable("agent_no_colon")


def test_clean_drops_none_only():
    assert _clean({"a": 0, "b": None, "c": "", "d": False, "e": []}) == {
        "a": 0, "c": "", "d": False, "e": [],
    }


# ── env resolution ────────────────────────────────────────────────


def test_contract_ids_csv(monkeypatch):
    monkeypatch.setenv("CEKI_CONTRACT_IDS", "14,21")
    assert contract_ids_from_env() == ["14", "21"]


def test_contract_ids_bracketed(monkeypatch):
    monkeypatch.setenv("CEKI_CONTRACT_IDS", "[14,21]")
    assert contract_ids_from_env() == ["14", "21"]


def test_contract_ids_json(monkeypatch):
    monkeypatch.setenv("CEKI_CONTRACT_IDS", "[14, 21]")
    assert contract_ids_from_env() == ["14", "21"]


def test_contract_ids_empty(monkeypatch):
    monkeypatch.delenv("CEKI_CONTRACT_IDS", raising=False)
    assert contract_ids_from_env() == []


def test_endpoint_override(monkeypatch):
    monkeypatch.setenv("CEKI_AGENT_MCP_ENDPOINT", "https://x.example/mcp/agent")
    monkeypatch.setenv("CEKI_AGENT_TOKEN", "tok")
    c = ContractClient()
    assert c.endpoint == "https://x.example/mcp/agent"


def test_endpoint_derived_from_api(monkeypatch):
    monkeypatch.delenv("CEKI_AGENT_MCP_ENDPOINT", raising=False)
    monkeypatch.setenv("CEKI_API_URL", "https://clawapi.ittribe.org")
    monkeypatch.setenv("CEKI_AGENT_TOKEN", "tok")
    c = ContractClient()
    assert c.endpoint == "https://clawapi.ittribe.org/mcp/agent"
    assert c.api_base == "https://clawapi.ittribe.org/api"


def test_token_agent_priority(monkeypatch):
    monkeypatch.setenv("CEKI_AGENT_TOKEN", "ag_xxx")
    monkeypatch.setenv("CEKI_API_KEY", "rental_yyy")
    c = ContractClient()
    assert c.token == "ag_xxx"


def test_token_fallback_to_api_key(monkeypatch):
    monkeypatch.delenv("CEKI_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("CEKI_API_KEY", "rental_yyy")
    c = ContractClient()
    assert c.token == "rental_yyy"


def test_no_token_raises(monkeypatch):
    monkeypatch.delenv("CEKI_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("CEKI_API_KEY", raising=False)
    http, _ = _http_mock(_mcp_text({"ok": True}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="")
    with pytest.raises(ContractError):
        c.list_contracts()


# ── MCP unwrapping ────────────────────────────────────────────────


def test_call_unwraps_text_json():
    http, _ = _http_mock(_mcp_text({"items": [1, 2]}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    assert c.list_contracts() == {"items": [1, 2]}


def test_call_returns_structured_content():
    http, _ = _http_mock({"result": {"structuredContent": {"k": "v"}}})
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    assert c.list_contracts() == {"k": "v"}


def test_call_non200_raises():
    http, _ = _http_mock({"error": "bad"}, status=500)
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    with pytest.raises(ContractError):
        c.list_contracts()


def test_call_jsonrpc_error_raises():
    http, _ = _http_mock({"error": {"code": -32000, "message": "nope"}})
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    with pytest.raises(ContractError):
        c.list_contracts()


# ── tool calls + payloads ─────────────────────────────────────────


def _captured_body(http: MagicMock) -> dict:
    return http.post.call_args.kwargs["json"]


def test_create_maps_tool_and_clean_payload():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="hello", duration=60, benefitable="agent:8")
    body = _captured_body(http)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "create-contract-event"
    args = body["params"]["arguments"]
    assert args == {
        "contract_id": 14,
        "label": "hello",
        "duration": 60,
        "benefitable": {"type": "agent", "value": 8},
    }


def test_comment_strips_undefined():
    http, _ = _http_mock(_mcp_text({"id": 99}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.comment(99, label="done", duration=30)
    args = _captured_body(http)["params"]["arguments"]
    assert args == {"event_id": 99, "label": "done", "duration": 30}
    assert "amount" not in args
    assert "currency" not in args
    assert "benefitable" not in args


def test_propose_maps_tool():
    http, _ = _http_mock(_mcp_text({}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.propose(7, status_id=200, label="L")
    body = _captured_body(http)
    assert body["params"]["name"] == "propose-correction"
    assert body["params"]["arguments"] == {"event_id": 7, "status_id": 200, "label": "L"}


def test_vote_payload_shape():
    http, _ = _http_mock(_mcp_text({}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.vote(7, [1, 2], True)
    body = _captured_body(http)
    assert body["params"]["name"] == "vote-correction"
    assert body["params"]["arguments"] == {"event_id": 7, "ids": [1, 2], "vote": True}


def test_history_tool_name():
    http, _ = _http_mock(_mcp_text([]))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.history(42)
    body = _captured_body(http)
    assert body["params"]["name"] == "get-event-history"
    assert body["params"]["arguments"] == {"event_id": 42}


# ── polling ───────────────────────────────────────────────────────


def test_poll_returns_list_directly():
    http, _ = _http_mock([{"x": 1}, {"x": 2}])
    c = ContractClient(
        client=http, endpoint="http://x/mcp/agent", api_base="http://x/api", token="t"
    )
    assert c.poll() == [{"x": 1}, {"x": 2}]


def test_poll_unwraps_notifications_key():
    http, _ = _http_mock({"notifications": [{"a": 1}]})
    c = ContractClient(
        client=http, endpoint="http://x/mcp/agent", api_base="http://x/api", token="t"
    )
    assert c.poll() == [{"a": 1}]


def test_poll_429_returns_empty():
    http, _ = _http_mock({"error": "rate"}, status=429)
    c = ContractClient(
        client=http, endpoint="http://x/mcp/agent", api_base="http://x/api", token="t"
    )
    assert c.poll() == []


def test_poll_other_error_raises():
    http, _ = _http_mock({"error": "boom"}, status=500)
    c = ContractClient(
        client=http, endpoint="http://x/mcp/agent", api_base="http://x/api", token="t"
    )
    with pytest.raises(ContractError):
        c.poll()


# ── CLI parser ────────────────────────────────────────────────────


def test_parser_contract_list():
    a = build_parser().parse_args(["contract", "list"])
    assert a.command == "contract" and a.contract_action == "list"


def test_parser_contract_create_flags():
    a = build_parser().parse_args([
        "contract", "create", "14",
        "--label", "X", "--status", "100", "--type", "2",
        "--kal-schedule", "9",
        "--duration", "60", "--amount", "1000", "--currency", "USD",
        "--benefitable", "agent:8", "--desc", "d",
    ])
    assert a.contract_action == "create"
    assert a.cid == 14
    assert a.label == "X"
    assert a.status == 100
    assert a.type == 2
    assert a.kal_schedule == 9
    assert a.duration == 60
    assert a.amount == 1000
    assert a.currency == "USD"
    assert a.benefitable == "agent:8"
    assert a.desc == "d"


def test_parser_contract_vote():
    a = build_parser().parse_args(["contract", "vote", "42", "--ids", "1,2", "--vote", "true"])
    assert a.eid == 42 and a.ids == "1,2" and a.vote == "true"


def test_parser_contract_watch_default():
    a = build_parser().parse_args(["contract", "watch"])
    assert a.interval == 8


def test_parser_contract_raw():
    a = build_parser().parse_args(["contract", "raw", "get-event", '{"event_id":1}'])
    assert a.tool == "get-event" and a.args == '{"event_id":1}'


def test_parser_contract_tasks_optional_cid():
    a = build_parser().parse_args(["contract", "tasks"])
    assert a.cid is None
    a = build_parser().parse_args(["contract", "tasks", "14"])
    assert a.cid == 14


# ── extended MCP schema coverage (timezone/data/start/end/date/limit) ──


def test_create_passes_timezone_and_data():
    http, _ = _http_mock(_mcp_text({"id": 5}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(
        14, label="L", timezone="Europe/Moscow",
        data={"foo": "bar"}, start="2026-06-20 10:00:00",
    )
    args = _captured_body(http)["params"]["arguments"]
    assert args["timezone"] == "Europe/Moscow"
    assert args["data"] == {"foo": "bar"}
    assert args["start"] == "2026-06-20 10:00:00"


def test_comment_passes_start_end_date():
    http, _ = _http_mock(_mcp_text({}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.comment(7, label="x", start="s", end="e", date="2026-06-18")
    args = _captured_body(http)["params"]["arguments"]
    assert args["start"] == "s" and args["end"] == "e" and args["date"] == "2026-06-18"


def test_propose_passes_start_end_date():
    http, _ = _http_mock(_mcp_text({}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.propose(7, status_id=200, start="s", end="e", date="2026-06-18")
    args = _captured_body(http)["params"]["arguments"]
    assert args["start"] == "s" and args["end"] == "e" and args["date"] == "2026-06-18"


def test_history_passes_limit():
    http, _ = _http_mock(_mcp_text([]))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.history(42, limit=10)
    args = _captured_body(http)["params"]["arguments"]
    assert args == {"event_id": 42, "limit": 10}


def test_history_no_limit_omits_field():
    http, _ = _http_mock(_mcp_text([]))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.history(42)
    args = _captured_body(http)["params"]["arguments"]
    assert args == {"event_id": 42}


def test_parser_history_limit():
    a = build_parser().parse_args(["contract", "history", "5", "--limit", "20"])
    assert a.eid == 5 and a.limit == 20


def test_parser_create_timezone_data():
    a = build_parser().parse_args([
        "contract", "create", "14", "--label", "X",
        "--timezone", "UTC", "--data", '{"k":1}',
    ])
    assert a.timezone == "UTC" and a.data == '{"k":1}'


def test_parser_comment_start_end_date():
    a = build_parser().parse_args([
        "contract", "comment", "5",
        "--start", "s", "--end", "e", "--date", "d",
    ])
    assert a.start == "s" and a.end == "e" and a.date == "d"


def test_parser_propose_start_end_date():
    a = build_parser().parse_args([
        "contract", "propose", "5",
        "--start", "s", "--end", "e", "--date", "d",
    ])
    assert a.start == "s" and a.end == "e" and a.date == "d"


# ── participants[] payload on create (task 2494, back/2485) ───────


def test_create_reviewer_folds_into_participants():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", reviewer="agent:9")
    args = _captured_body(http)["params"]["arguments"]
    assert args["participants"] == [{"value": 9, "type": "agent", "role_id": 5}]
    assert "reviewer" not in args
    assert "qa" not in args


def test_create_qa_folds_into_participants():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", qa="user:42")
    args = _captured_body(http)["params"]["arguments"]
    assert args["participants"] == [{"value": 42, "type": "user", "role_id": 6}]
    assert "reviewer" not in args
    assert "qa" not in args


def test_create_reviewer_and_qa_both_in_participants():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", reviewer="agent:9", qa="agent:12")
    args = _captured_body(http)["params"]["arguments"]
    assert "reviewer" not in args
    assert "qa" not in args
    by_role = {p["role_id"]: p for p in args["participants"]}
    assert by_role[5] == {"value": 9, "type": "agent", "role_id": 5}
    assert by_role[6] == {"value": 12, "type": "agent", "role_id": 6}
    assert len(args["participants"]) == 2


def test_create_no_reviewer_no_qa_omits_participants():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", benefitable="agent:8")
    args = _captured_body(http)["params"]["arguments"]
    assert "participants" not in args
    assert "reviewer" not in args
    assert "qa" not in args
    assert args["benefitable"] == {"type": "agent", "value": 8}


def test_create_benefitable_and_billable_stay_top_level():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", benefitable="agent:8", reviewer="agent:9")
    args = _captured_body(http)["params"]["arguments"]
    assert args["benefitable"] == {"type": "agent", "value": 8}
    assert args["participants"] == [{"value": 9, "type": "agent", "role_id": 5}]


def test_parser_create_reviewer_and_qa():
    a = build_parser().parse_args([
        "contract", "create", "14", "--label", "X",
        "--benefitable", "agent:8",
        "--reviewer", "agent:9",
        "--qa", "agent:12",
    ])
    assert a.benefitable == "agent:8"
    assert a.reviewer == "agent:9"
    assert a.qa == "agent:12"


def test_parser_create_participant_repeated():
    a = build_parser().parse_args([
        "contract", "create", "14", "--label", "X",
        "--participant", "agent:5:reviewer",
        "--participant", "user:7:qa",
    ])
    assert a.participant == ["agent:5:reviewer", "user:7:qa"]


def test_parse_participant_reviewer_shortcut():
    from ceki_sdk.cli import _parse_participant
    assert _parse_participant("agent:5:reviewer") == {
        "value": 5, "type": "agent", "role_id": 5,
    }


def test_parse_participant_qa_shortcut():
    from ceki_sdk.cli import _parse_participant
    assert _parse_participant("user:7:qa") == {
        "value": 7, "type": "user", "role_id": 6,
    }


def test_parse_participant_numeric_role():
    from ceki_sdk.cli import _parse_participant
    assert _parse_participant("agent:5:role:42") == {
        "value": 5, "type": "agent", "role_id": 42,
    }


def test_parse_participant_unknown_role_raises():
    from ceki_sdk.cli import _parse_participant
    with pytest.raises(ValueError, match="unknown role"):
        _parse_participant("agent:5:bogus")


def test_parse_participant_bad_type_raises():
    from ceki_sdk.cli import _parse_participant
    with pytest.raises(ValueError, match="type"):
        _parse_participant("robot:5:reviewer")


def test_create_reviewer_plus_participant_stacks():
    """--reviewer agent:9 + --participant agent:5:reviewer → two role_id=5 entries."""
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(
        14, label="L",
        reviewer="agent:9",
        participants=[{"value": 5, "type": "agent", "role_id": 5}],
    )
    args = _captured_body(http)["params"]["arguments"]
    parts = args["participants"]
    assert len(parts) == 2
    assert all(p["role_id"] == 5 for p in parts)
    values = sorted(p["value"] for p in parts)
    assert values == [5, 9]

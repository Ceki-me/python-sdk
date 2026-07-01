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
    # task 2936 — `description` must never appear on a comment payload.
    assert "description" not in args


def test_comment_long_multiline_body_in_label_no_description():
    """Regression guard for task 2936 (duplicate-comment bug).

    A comment's body lives ENTIRELY in `label` (events.label is
    unbounded TEXT). `description` is not part of the comment() API and
    must never end up on the wire — the web renderer would show both
    `label` and `description`, duplicating the body.
    """
    http, _ = _http_mock(_mcp_text({"id": 99}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    body = (
        "long body across\nmultiple lines exceeding 60 characters in "
        "total length here for sure"
    )
    c.comment(99, label=body)
    args = _captured_body(http)["params"]["arguments"]
    # Full body, untruncated, multi-line preserved.
    assert args["label"] == body
    assert len(body) > 60  # sanity — the body really is over 60 chars
    # No description key at all on the wire.
    assert "description" not in args


def test_comment_no_description_kwarg():
    """The comment() public API must not accept `description=`.

    Dropping this kwarg is the API-level breaking change for 2.30.0:
    a comment's body lives in `label`. Anyone still passing
    `description=` gets a TypeError at call time, which is the loud
    failure we want.
    """
    c = ContractClient(endpoint="http://x/mcp/agent", token="t")
    with pytest.raises(TypeError):
        c.comment(99, label="x", description="y")  # type: ignore[call-arg]


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


def test_my_events_uses_get_my_events_tool():
    """Pin the new wire name for the contract-task plate feed.

    Backend swapped: the wire tool that returns contract events
    assigned to me is now `get-my-events` (formerly `get-my-jobs`).
    The Python method is `ContractClient.my_events()`.
    """
    http, _ = _http_mock(_mcp_text([]))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.my_events()
    body = _captured_body(http)
    assert body["params"]["name"] == "get-my-events"
    assert body["params"]["arguments"] == {}


def test_my_jobs_uses_get_my_jobs_tool_for_hire_schedules():
    """Pin the reused wire name for the hire-schedules listings feed.

    Backend swapped: `get-my-jobs` (the wire tool) now returns the
    hire schedules I posted (type 3) — what was previously served by
    `get-hire-jobs`. The Python method `ContractClient.my_jobs()`
    targets this new semantic; for contract events use `my_events()`.
    """
    http, _ = _http_mock(_mcp_text([]))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.my_jobs()
    body = _captured_body(http)
    assert body["params"]["name"] == "get-my-jobs"
    assert body["params"]["arguments"] == {}


def test_no_hire_jobs_wire_name_anywhere():
    """The old `get-hire-jobs` wire name must be gone from the SDK map."""
    from ceki_sdk.contract import _TOOL_MAP
    assert "get-hire-jobs" not in _TOOL_MAP.values()
    # And the new entries are present under the right sugar keys.
    assert _TOOL_MAP["my-events"] == "get-my-events"
    assert _TOOL_MAP["my-jobs"] == "get-my-jobs"


def test_parser_contract_my_events():
    a = build_parser().parse_args(["contract", "my-events"])
    assert a.command == "contract" and a.contract_action == "my-events"


def test_parser_contract_my_jobs_still_present():
    """`contract my-jobs` is still a valid CLI action (now hire schedules)."""
    a = build_parser().parse_args(["contract", "my-jobs"])
    assert a.command == "contract" and a.contract_action == "my-jobs"


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


# ── users[] payload on create (task 2494, back/2542 — renamed from
#    participants[]) ────────────────────────────────────────────────


def test_create_reviewer_folds_into_users():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", reviewer="agent:9")
    args = _captured_body(http)["params"]["arguments"]
    assert args["users"] == [
        {"participable_id": 9, "type": "agent", "role_id": 5}
    ]
    assert "reviewer" not in args
    assert "qa" not in args
    assert "participants" not in args


def test_create_qa_folds_into_users():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", qa="user:42")
    args = _captured_body(http)["params"]["arguments"]
    assert args["users"] == [
        {"participable_id": 42, "type": "user", "role_id": 6}
    ]
    assert "reviewer" not in args
    assert "qa" not in args
    assert "participants" not in args


def test_create_reviewer_and_qa_both_in_users():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", reviewer="agent:9", qa="agent:12")
    args = _captured_body(http)["params"]["arguments"]
    assert "reviewer" not in args
    assert "qa" not in args
    assert "participants" not in args
    by_role = {p["role_id"]: p for p in args["users"]}
    assert by_role[5] == {
        "participable_id": 9, "type": "agent", "role_id": 5,
    }
    assert by_role[6] == {
        "participable_id": 12, "type": "agent", "role_id": 6,
    }
    assert len(args["users"]) == 2


def test_create_users_uses_participable_id_keys():
    """Regression guard for the 422 element-shape bug.

    EventController users[] validation (back/2542; previously named
    participants[]) requires `participable_id` + `participable_type` +
    `role_id` keys on each element. The earlier shape
    {value, type, role_id} was rejected with HTTP 422
    (`users.0.participable_id field is required`). This test pins the
    correct shape so the bug can't sneak back.
    """
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", reviewer="agent:9", qa="agent:12")
    parts = _captured_body(http)["params"]["arguments"]["users"]
    assert len(parts) == 2
    for p in parts:
        # correct keys present
        assert "participable_id" in p
        assert "type" in p
        assert "role_id" in p
        # forbidden legacy keys absent
        assert "value" not in p
        assert "participable_type" not in p
    by_role = {p["role_id"]: p for p in parts}
    assert by_role[5]["participable_id"] == 9
    assert by_role[5]["type"] == "agent"
    assert by_role[6]["participable_id"] == 12
    assert by_role[6]["type"] == "agent"


def test_create_uses_users_field_not_participants():
    """Regression guard pinning the wire-key rename.

    back/2542 renamed the role-attachment array on the wire from
    `participants` to `users`. This test asserts the emitted payload
    carries `users` and NOT `participants`, so we don't slip back.
    """
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", reviewer="agent:9", qa="user:42")
    args = _captured_body(http)["params"]["arguments"]
    assert "users" in args
    assert "participants" not in args


def test_create_no_reviewer_no_qa_omits_users():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", benefitable="agent:8")
    args = _captured_body(http)["params"]["arguments"]
    assert "users" not in args
    assert "participants" not in args
    assert "reviewer" not in args
    assert "qa" not in args
    assert args["benefitable"] == {"type": "agent", "value": 8}


def test_create_benefitable_and_billable_stay_top_level():
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(14, label="L", benefitable="agent:8", reviewer="agent:9")
    args = _captured_body(http)["params"]["arguments"]
    # benefitable is a different parser — stays {type, value}.
    assert args["benefitable"] == {"type": "agent", "value": 8}
    assert args["users"] == [
        {"participable_id": 9, "type": "agent", "role_id": 5}
    ]
    assert "participants" not in args


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
        "participable_id": 5, "type": "agent", "role_id": 5,
    }


def test_parse_participant_qa_shortcut():
    from ceki_sdk.cli import _parse_participant
    assert _parse_participant("user:7:qa") == {
        "participable_id": 7, "type": "user", "role_id": 6,
    }


def test_parse_participant_numeric_role():
    from ceki_sdk.cli import _parse_participant
    assert _parse_participant("agent:5:role:42") == {
        "participable_id": 5, "type": "agent", "role_id": 42,
    }


def test_parse_participant_unknown_role_raises():
    from ceki_sdk.cli import _parse_participant
    with pytest.raises(ValueError, match="unknown role"):
        _parse_participant("agent:5:bogus")


def test_parse_participant_bad_type_raises():
    from ceki_sdk.cli import _parse_participant
    with pytest.raises(ValueError, match="type"):
        _parse_participant("robot:5:reviewer")


# ── progress (status correction + comment in one shot) ───────────


def test_progress_calls_propose_then_comment(monkeypatch):
    """progress(eid, status=222, desc='r') → propose(status_id=222) then comment(label='r').

    The full body goes into `label`. `description` is never set on a
    comment — the UI renders `label` AND `description`, so doing both
    would visibly duplicate the body.
    """
    c = ContractClient(endpoint="http://x/mcp/agent", token="t")
    calls: list[tuple[str, tuple, dict]] = []

    def fake_propose(self, event_id, **kw):
        calls.append(("propose", (event_id,), kw))
        return {"applied": True, "id": 1}

    def fake_comment(self, event_id, **kw):
        calls.append(("comment", (event_id,), kw))
        return {"id": 2}

    monkeypatch.setattr(ContractClient, "propose", fake_propose)
    monkeypatch.setattr(ContractClient, "comment", fake_comment)

    result = c.progress(99, status=222, desc="r")

    assert [name for name, _, _ in calls] == ["propose", "comment"]
    assert calls[0][1] == (99,)
    assert calls[0][2] == {"status_id": 222}
    assert calls[1][1] == (99,)
    assert calls[1][2] == {"label": "r"}
    assert "description" not in calls[1][2]
    assert result == {
        "status_correction": {"applied": True, "id": 1},
        "comment": {"id": 2},
    }


def test_progress_without_status_only_comments(monkeypatch):
    """progress(eid, desc=...) without status → ONLY comment, propose never called."""
    c = ContractClient(endpoint="http://x/mcp/agent", token="t")
    propose_calls: list = []
    comment_calls: list = []

    def fake_propose(self, event_id, **kw):
        propose_calls.append((event_id, kw))
        return {"applied": True}

    def fake_comment(self, event_id, **kw):
        comment_calls.append((event_id, kw))
        return {"id": 7}

    monkeypatch.setattr(ContractClient, "propose", fake_propose)
    monkeypatch.setattr(ContractClient, "comment", fake_comment)

    result = c.progress(99, desc="just an update")

    assert propose_calls == []
    assert comment_calls == [(99, {"label": "just an update"})]
    assert "description" not in comment_calls[0][1]
    assert result == {"status_correction": None, "comment": {"id": 7}}


def test_progress_never_passes_desc_to_propose(monkeypatch):
    """Regression guard: --desc must NEVER reach propose (would overwrite spec)."""
    c = ContractClient(endpoint="http://x/mcp/agent", token="t")
    propose_kwargs: dict = {}

    def fake_propose(self, event_id, **kw):
        propose_kwargs.update(kw)
        return {"applied": True}

    def fake_comment(self, event_id, **kw):
        return {"id": 1}

    monkeypatch.setattr(ContractClient, "propose", fake_propose)
    monkeypatch.setattr(ContractClient, "comment", fake_comment)

    c.progress(99, status=222, desc="this is a progress report, NOT a spec")

    assert "status_id" in propose_kwargs
    assert "desc" not in propose_kwargs
    assert "description" not in propose_kwargs
    assert "label" not in propose_kwargs


def test_progress_puts_full_desc_in_label_no_description(monkeypatch):
    """Regression guard for the duplicate-comment bug (task 2936).

    progress() used to send `label = desc.splitlines()[0][:60]` AND the
    full text as `description`. The UI renders both fields, so the
    truncated prefix appeared on top of the full body — visible
    duplication.

    The fix: full body goes into `label` (events.label is unbounded
    TEXT), and `description` is never set on a comment.
    """
    c = ContractClient(endpoint="http://x/mcp/agent", token="t")
    comment_kwargs: dict = {}

    def fake_propose(self, event_id, **kw):
        return {"applied": True}

    def fake_comment(self, event_id, **kw):
        comment_kwargs.update(kw)
        return {"id": 1}

    monkeypatch.setattr(ContractClient, "propose", fake_propose)
    monkeypatch.setattr(ContractClient, "comment", fake_comment)

    long_desc = "Long report\nwith multiple\nlines that go well past sixty characters total"
    c.progress(99, desc=long_desc)

    # Full body in label, untruncated, multi-line preserved.
    assert comment_kwargs["label"] == long_desc
    # `description` MUST NOT be passed — UI duplication guard.
    assert "description" not in comment_kwargs


def test_progress_wire_payload_label_only(monkeypatch):
    """End-to-end wire shape: progress() comment arguments contain
    `label` with the full body and NO `description` key.
    """
    http, _ = _http_mock(_mcp_text({"id": 5}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.progress(99, status=222, desc="Long report\nwith multiple\nlines")

    # Two POSTs: propose, then comment. Comment is the last one.
    last_body = http.post.call_args.kwargs["json"]
    assert last_body["params"]["name"] == "comment"
    args = last_body["params"]["arguments"]
    assert args["label"] == "Long report\nwith multiple\nlines"
    assert "description" not in args


def test_parser_progress_full():
    a = build_parser().parse_args([
        "contract", "progress", "99", "--status", "222", "--desc", "did stuff",
    ])
    assert a.contract_action == "progress"
    assert a.eid == 99
    assert a.status == 222
    assert a.desc == "did stuff"


def test_parser_progress_no_status():
    a = build_parser().parse_args([
        "contract", "progress", "99", "--desc", "just a note",
    ])
    assert a.contract_action == "progress"
    assert a.eid == 99
    assert a.status is None
    assert a.desc == "just a note"


def test_parser_progress_missing_desc_fails():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["contract", "progress", "99"])


def test_cli_dispatch_progress(monkeypatch, capsys):
    """End-to-end: `ceki contract progress 99 --status 222 --desc x` calls client.progress."""
    from ceki_sdk import cli as cli_module

    captured: dict = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def progress(self, eid, *, status, desc):
            captured["eid"] = eid
            captured["status"] = status
            captured["desc"] = desc
            return {"status_correction": {"ok": 1}, "comment": {"ok": 2}}

    monkeypatch.setattr(cli_module, "_contract_client", lambda: FakeClient())

    parser = cli_module.build_parser()
    args = parser.parse_args(["contract", "progress", "99", "--status", "222", "--desc", "x"])
    rc = cli_module._cmd_contract(args)

    assert rc == 0
    assert captured == {"eid": 99, "status": 222, "desc": "x"}


# ── call-human (task 4019) ────────────────────────────────────────


def test_call_human_calls_correct_tool():
    """`client.call_human(99, 'stuck', 'body')` → tool name 'call-human'
    on the wire, with `{event_id, kind, desc}` arguments.
    """
    http, _ = _http_mock(_mcp_text({
        "recipients": [], "dispatched": 0, "deep_link": "u", "kind": "stuck",
    }))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.call_human(99, "stuck", "body")
    body = _captured_body(http)
    assert body["params"]["name"] == "call-human"
    assert body["params"]["arguments"] == {
        "event_id": 99, "kind": "stuck", "desc": "body",
    }


def test_call_human_rejects_bad_kind():
    """Any kind outside {'input','review','stuck'} → ValueError."""
    c = ContractClient(endpoint="http://x/mcp/agent", token="t")
    with pytest.raises(ValueError, match="kind must be"):
        c.call_human(99, "urgent", "body")
    with pytest.raises(ValueError, match="kind must be"):
        c.call_human(99, "", "body")


def test_parser_contract_call_human_ok():
    a = build_parser().parse_args([
        "contract", "call-human", "42", "--kind", "review", "--desc", "why",
    ])
    assert a.contract_action == "call-human"
    assert a.event_id == 42
    assert a.kind == "review"
    assert a.desc == "why"


def test_parser_contract_call_human_missing_kind_fails():
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "contract", "call-human", "42", "--desc", "why",
        ])


def test_parser_contract_call_human_missing_desc_fails():
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "contract", "call-human", "42", "--kind", "review",
        ])


def test_parser_contract_call_human_missing_event_id_fails():
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "contract", "call-human", "--kind", "review", "--desc", "why",
        ])


def test_parser_contract_call_human_bad_kind_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "contract", "call-human", "42", "--kind", "urgent", "--desc", "why",
        ])


def test_cli_dispatch_call_human(monkeypatch, capsys):
    """End-to-end: CLI `contract call-human 42 --kind review --desc why`
    hits `client.call_human(42, 'review', 'why')` exactly.
    """
    from ceki_sdk import cli as cli_module

    captured: dict = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call_human(self, event_id, kind, desc):
            captured["event_id"] = event_id
            captured["kind"] = kind
            captured["desc"] = desc
            return {
                "recipients": [{"user_id": 1, "label": "L", "reason": "R"}],
                "dispatched": 1,
                "deep_link": "https://ex/e/42",
                "kind": "review",
            }

    monkeypatch.setattr(cli_module, "_contract_client", lambda: FakeClient())

    parser = cli_module.build_parser()
    args = parser.parse_args([
        "contract", "call-human", "42", "--kind", "review", "--desc", "why",
    ])
    rc = cli_module._cmd_contract(args)

    assert rc == 0
    assert captured == {"event_id": 42, "kind": "review", "desc": "why"}


def test_create_reviewer_plus_participant_stacks():
    """--reviewer agent:9 + --participant agent:5:reviewer → two role_id=5 entries.

    The `participants` kwarg is the stable Python API for callers
    (CLI feeds it from --participant); on the wire both feed into
    the `users` array (back/2542 rename).
    """
    http, _ = _http_mock(_mcp_text({"id": 1}))
    c = ContractClient(client=http, endpoint="http://x/mcp/agent", token="t")
    c.create(
        14, label="L",
        reviewer="agent:9",
        participants=[
            {"participable_id": 5, "type": "agent", "role_id": 5}
        ],
    )
    args = _captured_body(http)["params"]["arguments"]
    parts = args["users"]
    assert "participants" not in args
    assert len(parts) == 2
    assert all(p["role_id"] == 5 for p in parts)
    values = sorted(p["participable_id"] for p in parts)
    assert values == [5, 9]

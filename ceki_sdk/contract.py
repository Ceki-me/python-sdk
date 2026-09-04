"""Client for /mcp/agent contract tools (1:1 port of ceki-agent.js)."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import httpx

from ._config import default_api_url

# Contract role IDs (back/2542 users[] payload — renamed from participants[]).
ROLE_REVIEWER = 5
ROLE_QA = 6


def _benefitable(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parts = str(value).split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"benefitable must be 'type:id', got: {value!r}")
    btype, bid = parts
    return {"type": btype, "value": int(bid)}


def _participant(value: str | None, role_id: int) -> dict[str, Any] | None:
    """Parse 'agent:8' / 'user:61' into {participable_id, type, role_id}.

    Wire shape declared by the create-contract-event MCP tool schema:
    `participable_id` + `type` (short token: 'agent' or 'user') +
    `role_id`. The MCP tool drops any field it does not know about, so
    sending `participable_type` (FQCN) silently loses the type and the
    backend membership lookup defaults to user → misleading 422
    "Participant must be a member of the contract". Send `type`.
    """
    base = _benefitable(value)
    if base is None:
        return None
    return {
        "participable_id": base["value"],
        "type": base["type"],
        "role_id": role_id,
    }


def _clean(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if v is not None}


def _word_boundary(text: str, limit: int) -> int:
    """Find the last space position before limit for clean word break.

    Returns the index of the last space within `limit`, or `limit` itself
    if no space is found (forces a hard cut). Used to avoid splitting
    mid-word when truncating long text.
    """
    i = text.rfind(" ", 0, limit)
    return i if i > 0 else limit


def _split_label_desc(
    label: str | None, description: str | None, limit: int = 1024
) -> tuple[str | None, str | None]:
    """Split long text between label and description at word boundary.

    Rules:
    - Both present (label AND description): returned as-is.
    - Only label: len<=limit → (label, None); >limit → cut at word boundary,
      return (label[:cut], label[cut:].strip()).
    - Only description (no label): len<=limit → (description, None);
      >limit → cut at word boundary, return (desc[:cut], desc[cut:].strip()).
    - Both None: (None, None).

    Args:
        label: Primary text (events.label).
        description: Secondary text (events.description).
        limit: Max length for the first component before split (default 1024).

    Returns:
        (label_out, description_out) where label_out is at most `limit`
        characters (cut at word boundary when splitting).
    """
    if label is not None and description is not None:
        # Both explicitly provided – send as-is
        return label, description

    if label is not None:
        if len(label) <= limit:
            return label, None
        # Too long – split at word boundary
        cut = _word_boundary(label, limit)
        return label[:cut], label[cut:].strip() or None

    if description is not None:
        if len(description) <= limit:
            return description, None
        # Too long – split at word boundary
        cut = _word_boundary(description, limit)
        return description[:cut], description[cut:].strip() or None

    return None, None


def contract_ids_from_env() -> list[str]:
    raw = (os.getenv("CEKI_CONTRACT_IDS") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return [s.strip() for s in raw.replace("[", "").replace("]", "").split(",") if s.strip()]


def _resolve_endpoint() -> str:
    override = os.getenv("CEKI_AGENT_MCP_ENDPOINT")
    if override:
        return override.rstrip("/")
    base = default_api_url().rstrip("/")
    return f"{base}/mcp/agent"


def _resolve_api_base() -> str:
    override = os.getenv("CEKI_API_BASE")
    if override:
        return override.rstrip("/")
    base = default_api_url().rstrip("/")
    return f"{base}/api"


def _resolve_token() -> str:
    return os.getenv("CEKI_AGENT_TOKEN") or os.getenv("CEKI_API_KEY") or ""


# Wire names swapped on the backend:
#   get-my-jobs   (formerly contract tasks)      → get-my-events
#   get-hire-jobs (formerly posted hire jobs)    → get-my-jobs
# The two sugar keys reflect the new, non-cross-contaminated semantics:
#   "my-events" = contract events assigned to me  (the plate feed)
#   "my-jobs"   = hire schedules I posted (type 3) (the listings feed)
_TOOL_MAP = {
    "list": "get-my-contracts",
    "members": "get-contract-members",
    "tasks": "get-contract-events",
    "my-events": "get-my-events",
    "my-jobs": "get-my-jobs",
    "task": "get-event",
    "children": "get-event-children",
    "history": "get-event-history",
    "create": "create-contract-event",
    "comment": "comment",
    "propose": "propose-correction",
    "vote": "vote-correction",
}


class ContractError(Exception):
    pass


class ContractClient:
    """JSON-RPC MCP client for /mcp/agent + REST polling.

    Mirrors `ceki-agent.js` behavior. Reads env at construction unless
    explicit values are passed.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        token: str | None = None,
        api_base: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = (endpoint or _resolve_endpoint()).rstrip("/")
        self.api_base = (api_base or _resolve_api_base()).rstrip("/")
        self.token = token if token is not None else _resolve_token()
        self._timeout = timeout
        self._client = client
        self._own_client = client is None

    def close(self) -> None:
        if self._own_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "ContractClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ContractError("agent token not set (CEKI_AGENT_TOKEN or CEKI_API_KEY)")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params}
        resp = self._http().post(self.endpoint, headers=self._headers(), json=body)
        try:
            parsed = resp.json()
        except Exception:
            parsed = {"raw": resp.text}
        if resp.status_code != 200:
            snippet = json.dumps(parsed)[:400]
            raise ContractError(f"HTTP {resp.status_code}: {snippet}")
        return parsed

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Call MCP tool, unwrap content[].text (JSON-parsed) or structuredContent."""
        body = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        if body.get("error"):
            raise ContractError(f"{tool} → {json.dumps(body['error'])[:400]}")
        result = body.get("result") or {}
        content = result.get("content")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except Exception:
                return joined
        return result.get("structuredContent", result)

    def tools(self) -> Any:
        body = self._rpc("tools/list", {})
        result = body.get("result") or {}
        tools = result.get("tools")
        if isinstance(tools, list):
            return [t.get("name") for t in tools]
        return body

    def raw(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        # prompts/list and prompts/get are MCP protocol methods (JSON-RPC),
        # not tools. Call _rpc directly instead of tools/call.
        if tool.startswith("prompts/"):
            return self._rpc(tool, args or {})
        return self.call(tool, args)

    # ── domain helpers ────────────────────────────────────────────

    def list_contracts(self) -> Any:
        return self.call(_TOOL_MAP["list"], {})

    def members(self, contract_id: int) -> Any:
        return self.call(_TOOL_MAP["members"], {"contract_id": int(contract_id)})

    def tasks(self, contract_id: int) -> Any:
        """List contract events — auto-paginates ALL pages.

        Backend returns paginated ({data, current_page, last_page, total,
        per_page}); the old single-call returned only page 1 (50 of N),
        silently truncating. We follow last_page and merge data[].
        """
        cid = int(contract_id)
        tool = _TOOL_MAP["tasks"]
        out = self.call(tool, {"contract_id": cid})
        if not isinstance(out, dict) or not isinstance(out.get("data"), list):
            return out
        page = int(out.get("current_page", 1) or 1)
        last = int(out.get("last_page", 1) or 1)
        merged = list(out["data"])
        while page < last:
            page += 1
            nxt = self.call(tool, {"contract_id": cid, "page": page})
            if not isinstance(nxt, dict) or not isinstance(nxt.get("data"), list):
                break
            merged.extend(nxt["data"])
            last = int(nxt.get("last_page", last) or last)  # safety if backend shifts
        # dedup by id — backend pagination occasionally overlaps pages
        seen: set[Any] = set()
        uniq: list[Any] = []
        for e in merged:
            eid = e.get("id") if isinstance(e, dict) else None
            if eid is None or eid not in seen:
                if eid is not None:
                    seen.add(eid)
                uniq.append(e)
        out = dict(out)
        out["data"] = uniq
        out["current_page"] = 1
        out["last_page"] = 1
        out["per_page"] = len(uniq)
        return out

    def my_events(self) -> Any:
        """Contract events assigned to me — the agent's plate feed.

        Calls `get-my-events` (formerly `get-my-jobs`; backend renamed
        the wire tool when the listings feed reclaimed `get-my-jobs`).
        Auto-paginates ALL pages.
        """
        tool = _TOOL_MAP["my-events"]
        out = self.call(tool, {})
        if not isinstance(out, dict) or not isinstance(out.get("data"), list):
            return out
        page = int(out.get("current_page", 1) or 1)
        last = int(out.get("last_page", 1) or 1)
        merged = list(out["data"])
        while page < last:
            page += 1
            nxt = self.call(tool, {"page": page})
            if not isinstance(nxt, dict) or not isinstance(nxt.get("data"), list):
                break
            merged.extend(nxt["data"])
            last = int(nxt.get("last_page", last) or last)  # safety if backend shifts
        # dedup by id — backend pagination occasionally overlaps pages
        seen: set[Any] = set()
        uniq: list[Any] = []
        for e in merged:
            eid = e.get("id") if isinstance(e, dict) else None
            if eid is None or eid not in seen:
                if eid is not None:
                    seen.add(eid)
                uniq.append(e)
        out = dict(out)
        out["data"] = uniq
        out["current_page"] = 1
        out["last_page"] = 1
        out["per_page"] = len(uniq)
        return out

    def call_human(self, event_id: int, kind: str, desc: str) -> Any:
        """Escalate to a human up the event→parent→contract→schedule chain.

        Args:
            event_id: id of the event to escalate on (must exist).
            kind: 'input' | 'review' | 'stuck'.
            desc: what specifically the caller is stuck on.

        Returns a dict shaped
        ``{"recipients":[{"user_id","label","reason"},...], "dispatched":<int>,
        "deep_link":"<url>", "kind":"<kind>"}``.
        """
        if kind not in ("input", "review", "stuck"):
            raise ValueError(
                f"kind must be 'input' | 'review' | 'stuck', got {kind!r}"
            )
        return self.call("call-human", {
            "event_id": int(event_id),
            "kind": kind,
            "desc": desc,
        })

    def my_jobs(self) -> Any:
        """Hire schedules I posted (type 3) — the listings feed.

        Calls `get-my-jobs` (the wire name was reused for this semantic
        after the backend swap; previously this method returned contract
        events — use `my_events()` for that now).
        Auto-paginates ALL pages.
        """
        tool = _TOOL_MAP["my-jobs"]
        out = self.call(tool, {})
        if not isinstance(out, dict) or not isinstance(out.get("data"), list):
            return out
        page = int(out.get("current_page", 1) or 1)
        last = int(out.get("last_page", 1) or 1)
        merged = list(out["data"])
        while page < last:
            page += 1
            nxt = self.call(tool, {"page": page})
            if not isinstance(nxt, dict) or not isinstance(nxt.get("data"), list):
                break
            merged.extend(nxt["data"])
            last = int(nxt.get("last_page", last) or last)  # safety if backend shifts
        # dedup by id — backend pagination occasionally overlaps pages
        seen: set[Any] = set()
        uniq: list[Any] = []
        for e in merged:
            eid = e.get("id") if isinstance(e, dict) else None
            if eid is None or eid not in seen:
                if eid is not None:
                    seen.add(eid)
                uniq.append(e)
        out = dict(out)
        out["data"] = uniq
        out["current_page"] = 1
        out["last_page"] = 1
        out["per_page"] = len(uniq)
        return out

    def task(self, event_id: int) -> Any:
        return self.call(_TOOL_MAP["task"], {"event_id": int(event_id)})

    def children(self, event_id: int) -> Any:
        return self.call(_TOOL_MAP["children"], {"event_id": int(event_id)})

    def history(self, event_id: int, *, limit: int | None = None) -> Any:
        args = _clean({"event_id": int(event_id), "limit": limit})
        return self.call(_TOOL_MAP["history"], args)

    def create(
        self,
        contract_id: int,
        *,
        label: str,
        type_id: int | None = None,
        status_id: int | None = None,
        kal_schedule_id: int | None = None,
        start: str | None = None,
        end: str | None = None,
        timezone: str | None = None,
        date: str | None = None,
        duration: int | None = None,
        amount: int | None = None,
        currency: str | None = None,
        description: str | None = None,
        data: dict[str, Any] | None = None,
        benefitable: str | None = None,
        reviewer: str | None = None,
        qa: str | None = None,
        participants: list[dict[str, Any]] | None = None,
        tags: list[dict[str, Any]] | None = None,
        files: list[int | str | Path] | None = None,
    ) -> Any:
        # back/2542: reviewer/qa now live inside users[] (renamed from
        # participants[]). Element shape unchanged. The `participants`
        # kwarg name is kept as a stable Python API for callers, but on
        # the wire it is emitted under the `users` key.
        users: list[dict[str, Any]] = []
        rev = _participant(reviewer, ROLE_REVIEWER)
        if rev is not None:
            users.append(rev)
        qa_p = _participant(qa, ROLE_QA)
        if qa_p is not None:
            users.append(qa_p)
        if participants:
            users.extend(participants)

        files_resolved = self._resolve_files(files)
        args = _clean({
            "contract_id": int(contract_id),
            "label": label,
            "type_id": type_id,
            "status_id": status_id,
            "kal_schedule_id": kal_schedule_id,
            "start": start,
            "end": end,
            "timezone": timezone,
            "date": date,
            "duration": duration,
            "amount": amount,
            "currency": currency,
            "description": description,
            "data": data,
            "benefitable": _benefitable(benefitable),
            "users": users if users else None,
            "files": files_resolved,
            # back/3165: project tags live in events.settings.tags[]. `tags`
            # is CLI/SDK sugar — a bare list of {key,label?,color?} dicts —
            # emitted on the wire under the `settings` blob the backend expects.
            "settings": {"tags": tags} if tags else None,
        })
        return self.call(_TOOL_MAP["create"], args)

    def comment(
        self,
        event_id: int,
        *,
        label: str | None = None,
        description: str | None = None,
        type_id: int | None = None,
        status_id: int | None = None,
        start: str | None = None,
        end: str | None = None,
        date: str | None = None,
        duration: int | None = None,
        amount: int | None = None,
        currency: str | None = None,
        benefitable: str | None = None,
        files: list[int | str | Path] | None = None,
    ) -> Any:
        """Post a comment event.

        The comment body lives primarily in `label` (events.label is
        unbounded TEXT). `description` is now supported: when a long
        label or description is provided (>1024 chars), the text is split
        at a word boundary – the first part goes to `label`, the remainder
        to `description`. This matches backend API behavior and keeps
        the UI clean without visible duplication.

        `files` attaches user_files to the comment: pass user_files ids
        (int) or local paths (str/Path) which are uploaded first.
        """
        label_out, desc_out = _split_label_desc(label, description)
        files_resolved = self._resolve_files(files)
        args = _clean({
            "event_id": int(event_id),
            "label": label_out,
            "description": desc_out,
            "type_id": type_id,
            "status_id": status_id,
            "start": start,
            "end": end,
            "date": date,
            "duration": duration,
            "amount": amount,
            "currency": currency,
            "benefitable": _benefitable(benefitable),
            "files": files_resolved,
        })
        return self.call(_TOOL_MAP["comment"], args)

    def propose(
        self,
        event_id: int,
        *,
        status_id: int | None = None,
        label: str | None = None,
        description: str | None = None,
        start: str | None = None,
        end: str | None = None,
        date: str | None = None,
        duration: int | None = None,
        amount: int | None = None,
        currency: str | None = None,
        benefitable: str | None = None,
        files: list[int | str | Path] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        label_out, desc_out = _split_label_desc(label, description)
        files_resolved = self._resolve_files(files)
        args = _clean({
            "event_id": int(event_id),
            "status_id": status_id,
            "label": label_out,
            "description": desc_out,
            "start": start,
            "end": end,
            "date": date,
            "duration": duration,
            "amount": amount,
            "currency": currency,
            "benefitable": _benefitable(benefitable),
            "files": files_resolved,
            # back/2796: ProposeCorrectionTool persists settings (tags,
            # reply_to, blocked_by, do_after) onto the event. Forwarded
            # verbatim — only attached when the caller supplies it.
            "settings": settings,
        })
        return self.call(_TOOL_MAP["propose"], args)

    def progress(
        self,
        event_id: int,
        *,
        status: int | None = None,
        desc: str,
        files: list[int | str | Path] | None = None,
    ) -> dict[str, Any]:
        """Status correction (optional) + progress comment in one shot.

        The event's own description is NOT touched. `--desc` becomes the
        body of a child comment-event, not a label/description overwrite
        on the parent event. Use this for Hand/QA/Reviewer progress
        reports — `propose --desc` would clobber the parent spec.

        `files` attaches user_files to the progress comment (ids or local
        paths — paths are uploaded first).
        """
        status_result: Any = None
        if status is not None:
            try:
                status_result = self.propose(event_id, status_id=int(status))
            except ContractError as exc:
                # If propose fails (e.g. invalid transition, voting rules),
                # still post the progress comment so --desc text is not lost.
                status_result = {"error": str(exc)}
        # events.label is unbounded TEXT — the full body lives there, and
        # `description` is never set on a comment (the UI renders both,
        # which would duplicate the body for SDK-posted comments).
        label = desc if (desc or "").strip() else "progress"
        comment_result = self.comment(event_id, label=label, files=files)
        return {"status_correction": status_result, "comment": comment_result}

    def vote(self, event_id: int, ids: list[int], vote: bool) -> Any:
        return self.call(_TOOL_MAP["vote"], {
            "event_id": int(event_id),
            "ids": [int(i) for i in ids],
            "vote": bool(vote),
        })

    # ── files ────────────────────────────────────────────────────

    def upload_file(
        self,
        file: bytes | str | Path,
        *,
        filename: str | None = None,
        mime: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file to the backend, returning its ``user_files`` record.

        Wraps the MCP ``upload-file`` tool (base64 over JSON-RPC). The
        returned ``id`` is what create()/comment()/propose() expect in
        their ``files`` param (backend: ``user_files.id``).

        Args:
            file: Local path OR raw bytes. Paths are read from disk.
            filename: Original filename with extension (defaults to the
                basename of a path, or a mime-derived name for raw bytes).
            mime: MIME type (e.g. image/png, application/pdf). Auto-guessed
                from the filename extension when omitted; the backend sniffs
                magic bytes as a final fallback.

        Returns:
            Dict shaped ``{"id", "name", "url", "size", "disk"}``.
        """
        if isinstance(file, (str, Path)):
            path = Path(file)
            data = path.read_bytes()
            if filename is None:
                filename = path.name
        else:
            data = bytes(file)
            if filename is None:
                filename = self._default_filename(mime)

        if not filename:
            raise ValueError("upload_file: could not derive a filename")

        if mime is None:
            guessed, _ = mimetypes.guess_type(filename)
            mime = guessed

        b64 = base64.b64encode(data).decode()
        result = self.call("upload-file", {
            "file_data": b64,
            "file_name": filename,
            "mime_type": mime,
        })
        if not isinstance(result, dict) or not result.get("id"):
            raise ContractError(f"upload-file returned no id: {result!r}")
        return result

    def _resolve_files(
        self, files: list[int | str | Path] | None
    ) -> list[int] | None:
        """Map a files[] arg to user_files ids.

        Ints pass through (already-uploaded ids); str/Path are uploaded
        via upload_file() first and replaced with the returned id.
        """
        if not files:
            return None
        ids: list[int] = []
        for f in files:
            if isinstance(f, int):
                ids.append(int(f))
                continue
            up = self.upload_file(f)
            fid = up.get("id")
            if not fid:
                raise ContractError(f"upload-file returned no id for {f!r}")
            ids.append(int(fid))
        return ids

    @staticmethod
    def _default_filename(mime: str | None) -> str:
        ext = (mimetypes.guess_extension(mime or "") or ".bin").lstrip(".")
        return f"file.{ext or 'bin'}"

    # ── polling (REST, not MCP) ───────────────────────────────────

    def poll(self) -> list[Any]:
        """GET /agent/polling. Returns [] on 429 (rate-limit, 10/min/token)."""
        resp = self._http().get(
            f"{self.api_base}/agent/polling",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        if resp.status_code == 429:
            return []
        if resp.status_code != 200:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise ContractError(f"polling HTTP {resp.status_code}: {str(body)[:300]}")
        body = resp.json()
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in ("notifications", "data", "items"):
                if key in body and isinstance(body[key], list):
                    return body[key]
        return []

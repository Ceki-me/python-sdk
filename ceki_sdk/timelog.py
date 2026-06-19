"""Client for /mcp/agent timelog tools (start/stop/check by event_id).

Thin wrapper around ContractClient — same transport, same auth, same env.
"""

from __future__ import annotations

from typing import Any

import httpx

from .contract import ContractClient, ContractError

_TOOL_MAP = {
    "start": "timelog-start",
    "stop": "timelog-stop",
    "check": "timelog-check",
}


class TimelogClient:
    """MCP timelog tools bound to an event_id."""

    def __init__(
        self,
        endpoint: str | None = None,
        token: str | None = None,
        api_base: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        contract: ContractClient | None = None,
    ) -> None:
        self._owns_contract = contract is None
        self._c = contract or ContractClient(
            endpoint=endpoint,
            token=token,
            api_base=api_base,
            client=client,
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owns_contract:
            self._c.close()

    def __enter__(self) -> "TimelogClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def start(self, event_id: int) -> Any:
        return self._c.call(_TOOL_MAP["start"], {"event_id": int(event_id)})

    def stop(self, event_id: int, label: str | None = None) -> Any:
        args: dict[str, Any] = {"event_id": int(event_id)}
        if label is not None:
            args["label"] = label
        return self._c.call(_TOOL_MAP["stop"], args)

    def check(self, event_id: int) -> Any:
        return self._c.call(_TOOL_MAP["check"], {"event_id": int(event_id)})


__all__ = ["TimelogClient", "ContractError"]

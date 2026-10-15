"""CloudBrowser subscription client (``ceki subscribe``).

Buys a browser subscription (type 3) on a browser group so the agent (or its
owner) gets rent access to that group's real browsers at $0/min. The backend
surface is the ``create-browser-subscription`` MCP tool (``POST /mcp/agent``),
which handles both discovery (no ``group_id`` → list groups with plans/geo)
and purchase (``group_id + plan_key + geo_key`` → subscription + invoice).

Auth: the MCP endpoint's ``OptionalAuthenticateAgent`` resolves agent ``ag_``
API keys (same as the ``/api/agent/subscription`` REST route behind the tool).
A user Sanctum token is NOT accepted — pass an agent key (``CEKI_AGENT_TOKEN``
or ``CEKI_API_KEY``), exactly like ``ceki contract``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._client import Client

from ._exceptions import CekiError

_TOOL = "create-browser-subscription"


class ClientSubscription:
    """MCP client for the ``create-browser-subscription`` tool.

    Lives on :attr:`Client.subscription`. Plain HTTP JSON-RPC calls to
    ``/mcp/agent`` — no relay / websocket involved.
    """

    def __init__(self, client: "Client") -> None:
        self._client = client

    def _rpc(self, args: dict[str, Any]) -> dict[str, Any]:
        from .contract import ContractClient
        # Reuse the existing MCP JSON-RPC client (same /mcp/agent endpoint,
        # token from CEKI_AGENT_TOKEN / CEKI_API_KEY).
        c = ContractClient()
        try:
            return c.call(_TOOL, args)
        except Exception as e:
            raise CekiError(f"subscription error: {e}") from e

    async def list_groups(self) -> list[dict[str, Any]]:
        """List available browser groups with their plans and geo options.

        Calling ``create-browser-subscription`` without ``group_id`` returns
        ``{groups: [{group_id, label, icon, plans, geo}]}``.
        """
        out = self._rpc({})
        groups = out.get("groups") if isinstance(out, dict) else None
        if not isinstance(groups, list):
            raise CekiError(f"subscription listing: unexpected response: {out!r}")
        return groups

    async def subscribe(
        self,
        group_id: int,
        *,
        plan_key: str | None = None,
        geo_key: str | None = None,
    ) -> dict[str, Any]:
        """Buy a CloudBrowser subscription on ``group_id``.

        Returns the tool payload: ``{subscription_id, payment_mode,
        price, currency, invoice_id?, note}``.
        """
        args: dict[str, Any] = {"group_id": int(group_id)}
        if plan_key:
            args["plan_key"] = plan_key
        if geo_key:
            args["geo_key"] = geo_key
        out = self._rpc(args)
        if not isinstance(out, dict):
            raise CekiError(f"subscription purchase: unexpected response: {out!r}")
        return out

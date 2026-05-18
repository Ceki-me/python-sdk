from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from ._browser import Browser

log = logging.getLogger(__name__)


class CaptchaResult:
    __slots__ = (
        "solved", "proof_message_id", "cancel_reason", "child_event_id",
        "_correction_id", "_browser", "_voted",
    )

    def __init__(
        self,
        *,
        solved: bool,
        child_event_id: int,
        proof_message_id: str | None = None,
        cancel_reason: str | None = None,
        correction_id: int | None = None,
        browser: Browser | None = None,
    ) -> None:
        self.solved = solved
        self.proof_message_id = proof_message_id
        self.cancel_reason = cancel_reason
        self.child_event_id = child_event_id
        self._correction_id = correction_id
        self._browser = browser
        self._voted = False

    async def accept_work(self) -> None:
        if self._voted or not self._correction_id or not self._browser:
            return
        self._voted = True
        client = self._browser._client
        headers = self._browser._api_headers()
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    f"{client.api_url}/api/kal/event/{self.child_event_id}/vote",
                    headers=headers,
                    json={"ids": [self._correction_id], "vote": True},
                )
                if not resp.is_success:
                    log.warning("accept_work vote failed: %s", resp.status_code)
        except Exception as exc:
            log.warning("accept_work failed: %s", exc)

    async def reject_work(self, reason: str | None = None) -> None:
        if self._voted or not self._correction_id or not self._browser:
            return
        self._voted = True
        client = self._browser._client
        headers = self._browser._api_headers()
        body: dict[str, Any] = {"ids": [self._correction_id], "vote": False}
        if reason:
            body["reason"] = reason
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    f"{client.api_url}/api/kal/event/{self.child_event_id}/vote",
                    headers=headers,
                    json=body,
                )
                if not resp.is_success:
                    log.warning("reject_work vote failed: %s", resp.status_code)
        except Exception as exc:
            log.warning("reject_work failed: %s", exc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "solved": self.solved,
            "proof_message_id": self.proof_message_id,
            "cancel_reason": self.cancel_reason,
            "child_event_id": self.child_event_id,
        }

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._browser import Browser

log = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSIONS = {1, 2}


class BrowserProfile:
    """Sugar layer for cookies + localStorage + sessionStorage + fingerprint snapshot/restore.

    Profile data stays agent-side. Server doesn't store it. Provider sees plaintext
    only during the active session (same as without profile).
    """

    SCHEMA_VERSION = 2

    def __init__(self, browser: "Browser") -> None:
        self._browser = browser

    async def export(
        self,
        *,
        domains: list[str] | None = None,
        include_session_storage: bool = True,
    ) -> dict[str, Any]:
        """Export current session state (cookies + localStorage + sessionStorage + fingerprint).

        domains: filter cookies by domain (e.g., ['.reddit.com', 'reddit.com']).
                 None = all cookies. localStorage/sessionStorage exported only
                 for the currently-loaded origin (CDP limitation).
        include_session_storage: set False to skip sessionStorage (e.g., to avoid
                 capturing tab-transient state).
        """
        try:
            fp_resp = await self._browser.send({"method": "Browser.getFingerprint"})
            fingerprint = fp_resp.get("fingerprint")
        except Exception:
            log.warning("profile.export: Browser.getFingerprint not available (extension too old?)")
            fingerprint = None

        cookies_resp = await self._browser.send({"method": "Network.getCookies"})
        cookies = cookies_resp.get("cookies", [])
        if domains is not None:
            allowed = set(domains)
            cookies = [c for c in cookies if c.get("domain") in allowed]

        local_storage = await self._eval_json("localStorage")
        session_storage: dict[str, str] = {}
        if include_session_storage:
            session_storage = await self._eval_json("sessionStorage")

        origin_resp = await self._browser.send({
            "method": "Runtime.evaluate",
            "params": {"expression": "location.origin", "returnByValue": True},
        })
        origin = origin_resp.get("result", {}).get("value")

        return {
            "schema_version": self.SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "origin": origin,
            "cookies": cookies,
            "localStorage": local_storage,
            "sessionStorage": session_storage,
        }

    async def import_(self, profile: dict[str, Any]) -> None:
        """Restore cookies + storage into the current session.

        Fingerprint is NOT applied here — it must be passed to client.rent(fingerprint=...)
        before the session starts. This method only restores cookies + storage.

        Cookies can be set before first navigation (they are domain-scoped).
        localStorage/sessionStorage require a document context — navigate to the
        target origin first, then call import_().
        """
        version = profile.get("schema_version", 1)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported profile schema_version={version}, expected one of {SUPPORTED_SCHEMA_VERSIONS}"
            )

        cookies = profile.get("cookies", [])
        if cookies:
            await self._browser.send({
                "method": "Network.setCookies",
                "params": {"cookies": cookies},
            })

        local_storage = profile.get("localStorage", {})
        if local_storage:
            await self._browser.send({
                "method": "Runtime.evaluate",
                "params": {
                    "expression": (
                        f"Object.entries({json.dumps(local_storage)})"
                        f".forEach(([k,v]) => localStorage.setItem(k, v))"
                    ),
                },
            })

        session_storage = profile.get("sessionStorage", {})
        if session_storage:
            await self._browser.send({
                "method": "Runtime.evaluate",
                "params": {
                    "expression": (
                        f"Object.entries({json.dumps(session_storage)})"
                        f".forEach(([k,v]) => sessionStorage.setItem(k, v))"
                    ),
                },
            })

    async def _eval_json(self, var: str) -> dict[str, str]:
        """JSON-stringify a storage object, return parsed dict. Empty dict on opaque origin."""
        resp = await self._browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": f"JSON.stringify(Object.fromEntries(Object.entries({var})))",
                "returnByValue": True,
            },
        })
        raw = resp.get("result", {}).get("value")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("profile.export: failed to parse %s", var)
            return {}

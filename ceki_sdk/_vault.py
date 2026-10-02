from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from ._browser import Browser
    from ._client import Client

log = logging.getLogger(__name__)

# Fields CDP/the extension accept on Network.setCookies. The raw jar comes back
# with extra diagnostic keys (priority, size, session, sourcePort,
# sourceScheme) that the browser rejects on set — keep only the settable ones.
SERIALIZABLE_COOKIE_FIELDS = (
    "name", "value", "domain", "path", "secure", "httpOnly",
    "expires", "sameSite", "session",
)


def sanitize_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-settable CDP fields from cookie objects.

    Network.getCookies returns extra fields (size, sourcePort, ...) that
    Network.setCookies rejects; the extension's VaultProfile contract only
    carries the settable subset. Returns a shallow copy per cookie.
    """
    out: list[dict[str, Any]] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if not c.get("name") or not isinstance(c.get("value"), str):
            continue
        out.append({k: c[k] for k in SERIALIZABLE_COOKIE_FIELDS if k in c})
    return out


def normalize_profile_for_vault(profile: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``profile.export()`` blob into the vault session ``data`` envelope.

    ``profile.export()`` returns a flat single-origin snapshot::

        {schema_version, fingerprint, origin, cookies,
         localStorage: {k: v}, sessionStorage: {k: v}}

    The vault API (and the extension's ``session.configure`` profile) expects a
    per-origin envelope::

        {fingerprint, cookies,
         localStorage: {<origin>: {...}}, sessionStorage: {<origin>: {...}},
         urls: [...], collectedAt: ISO}

    localStorage/sessionStorage are best-effort CDP captures of the currently
    loaded origin, so the snapshot's ``origin`` is the natural key. If the
    incoming storage is already per-origin (values are dicts — e.g. a vault
    envelope being re-uploaded), it is passed through untouched. The blob is
    already serialisable (no datetimes, no bytes); we don't unparse JSON strings.
    """
    envelope: dict[str, Any] = {
        "fingerprint": profile.get("fingerprint"),
        "cookies": sanitize_cookies(profile.get("cookies", [])),
    }

    origin = profile.get("origin") or "https://localhost"

    def _wrap(storage: Any) -> dict[str, Any]:
        if not isinstance(storage, dict):
            return {}
        already_by_origin = all(isinstance(v, dict) for v in storage.values())
        if already_by_origin:
            return storage
        return {origin: storage}

    envelope["localStorage"] = _wrap(profile.get("localStorage"))
    envelope["sessionStorage"] = _wrap(profile.get("sessionStorage"))

    urls = profile.get("urls")
    if not isinstance(urls, list):
        urls = [origin] if origin and origin != "https://localhost" else []
    envelope["urls"] = [u for u in urls if isinstance(u, str)]

    collected_at = profile.get("collectedAt")
    if not collected_at:
        collected_at = datetime.now(timezone.utc).isoformat()
    envelope["collectedAt"] = collected_at

    return envelope


def minimal_vault_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a fetched vault session ``data`` for ``session.configure``.

    Passes through per-origin localStorage/sessionStorage (the extension buffers
    them by origin) and sanitizes cookies. ``fingerprint`` is intentionally NOT
    copied into the profile — it is applied via the top-level ``fingerprint``
    configure field so the extension's existing fingerprint path stays the single
    source of truth. Unknown keys are dropped.
    """
    profile: dict[str, Any] = {}
    cookies = data.get("cookies")
    if isinstance(cookies, list):
        profile["cookies"] = sanitize_cookies(cookies)
    for key in ("localStorage", "sessionStorage"):
        storage = data.get(key)
        if isinstance(storage, dict):
            profile[key] = storage
    return profile


class VaultSession:
    """Minimal view of a vault session as returned by the API."""

    __slots__ = ("id", "label", "user_id", "data", "urls", "last_browser",
                 "created_at", "updated_at")

    def __init__(
        self,
        id: int,
        *,
        label: str | None = None,
        user_id: int | None = None,
        data: dict[str, Any] | None = None,
        urls: list[str] | None = None,
        last_browser: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.id = id
        self.label = label
        self.user_id = user_id
        self.data = data or {}
        self.urls = urls or []
        self.last_browser = last_browser
        self.created_at = created_at
        self.updated_at = updated_at

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "VaultSession":
        data = payload.get("data")
        # The backend may return the encrypted blob as-is (index serializes a
        # string) — only treat it as the decrypted profile when it's a dict.
        data_out = data if isinstance(data, dict) else {}
        # urls are top-level on the paginated index; inside the decrypted
        # envelope on show() — make both surfaces readable.
        urls = payload.get("urls")
        if not urls:
            data_urls = data_out.get("urls")
            urls = data_urls if isinstance(data_urls, list) else []
        raw_id = payload.get("id")
        return cls(
            id=int(raw_id) if raw_id is not None else 0,
            label=payload.get("label"),
            user_id=payload.get("user_id"),
            data=data_out,
            urls=urls,
            last_browser=payload.get("last_browser"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )


class ClientVault:
    """HTTP client for ``/api/vault/sessions`` (Vault 1-5 backend).

    Lives on :attr:`Client.vault`. All methods are plain ``httpx`` calls — no
    relay / websocket involvement — so they work before a session is rented.

    The vault routes are guarded by Sanctum (``auth:sanctum``) and resolve the
    token to a *user*; agent ``ag_`` keys are accepted by the same guard on the
    dev backend through ``AuthenticateSanctumOrAgent`` only if the backend later
    wires them in. For now, use a user Sanctum token as ``api_key`` for vault
    operations (the SDK's BS ``Authorization: Bearer`` header stays unchanged).
    """

    def __init__(self, client: "Client") -> None:
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._client.api_key}"}
        if self._client._basic_auth:
            raw = f"{self._client._basic_auth[0]}:{self._client._basic_auth[1]}"
            creds = base64.b64encode(raw.encode()).decode()
            headers["X-Basic-Auth"] = f"Basic {creds}"
        return headers

    async def list(self, **params: Any) -> list[VaultSession]:
        """List vault sessions (paginated by the backend, default 20/page)."""
        url = f"{self._client.api_url}/api/vault/sessions"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, headers=self._headers(), params=params or {"per_page": 20})
            resp.raise_for_status()
        body = resp.json()
        items = body.get("data", body) if isinstance(body, dict) else body
        return [VaultSession.from_payload(x) for x in items if isinstance(x, dict)]

    async def get(self, vault_session_id: int) -> VaultSession:
        """Fetch a vault session with its DECRYPTED ``data`` profile (show)."""
        url = f"{self._client.api_url}/api/vault/sessions/{vault_session_id}"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, headers=self._headers())
            resp.raise_for_status()
        return VaultSession.from_payload(resp.json())

    async def create(self, data: dict[str, Any], *, label: str | None = None) -> int:
        """Create a vault session; returns the new session id."""
        url = f"{self._client.api_url}/api/vault/sessions"
        payload: dict[str, Any] = {"data": data}
        if label is not None:
            payload["label"] = label
        async with httpx.AsyncClient() as http:
            resp = await http.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
        return int(resp.json().get("id"))

    async def update(
        self, vault_session_id: int, data: dict[str, Any], *, label: str | None = None
    ) -> VaultSession:
        """Overwrite an existing vault session's data (PUT, server encrypts)."""
        url = f"{self._client.api_url}/api/vault/sessions/{vault_session_id}"
        payload: dict[str, Any] = {"data": data}
        if label is not None:
            payload["label"] = label
        async with httpx.AsyncClient() as http:
            resp = await http.put(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
        return VaultSession.from_payload(resp.json())

    async def delete(self, vault_session_id: int) -> None:
        """Delete a vault session (owner only)."""
        url = f"{self._client.api_url}/api/vault/sessions/{vault_session_id}"
        async with httpx.AsyncClient() as http:
            resp = await http.delete(url, headers=self._headers())
            resp.raise_for_status()


class BrowserVault:
    """Vault sugar on a live :class:`Browser` (snapshot save / profile restore).

    Available as :attr:`Browser.vault`. Saving snapshots the current browser
    state through ``browser.profile.export()`` and pushes it to the vault.
    Restoring pulls a vault session and replays it into the current browser via
    ``session.configure(profile=...)`` (cookies immediately, storage buffered by
    the extension until first navigation to each origin) plus a fingerprint
    configure when the profile carries one.
    """

    def __init__(self, browser: "Browser") -> None:
        self._browser = browser
        self._client_vault = browser._client.vault

    async def save(
        self,
        *,
        label: str | None = None,
        include_session_storage: bool = True,
        domains: list[str] | None = None,
        overwrite: bool = False,
    ) -> int:
        """Snapshot the current browser into a vault session.

        Returns the vault session id. When the browser was rented with
        ``vault=<id>`` (a bound session), the snapshot overwrites that session
        (PUT). Otherwise a new session is created (POST). ``overwrite=True``
        forces a PUT against the bound id (no-op if no bound id).
        """
        profile = await self._browser.profile.export(
            include_session_storage=include_session_storage,
            domains=domains,
        )
        envelope = normalize_profile_for_vault(profile)
        bound_id = getattr(self._browser, "_vault_session_id", None)
        if overwrite or (bound_id is not None and not label):
            target = bound_id
            if target is None:
                raise ValueError("no bound vault session to overwrite; rent with vault=<id>")
            await self._client_vault.update(target, envelope, label=label)
            return int(target)
        return await self._client_vault.create(envelope, label=label)

    async def load(self, vault_session_id: int) -> dict[str, Any]:
        """Fetch a vault session's decrypted profile envelope (``data``)."""
        session = await self._client_vault.get(vault_session_id)
        if not session.data:
            raise ValueError(f"vault session {vault_session_id} has no profile data")
        return session.data

    async def restore(self, vault_session_id: int | dict[str, Any]) -> None:
        """Replay a vault profile into the current browser.

        ``vault_session_id`` may be an int (fetched from the API) or a raw
        profile envelope dict. Cookies are applied immediately (domain-scoped);
        localStorage/sessionStorage are buffered by the extension and flushed on
        first navigation to each origin; fingerprint (if present in the profile)
        is applied through ``session.configure``.
        """
        if isinstance(vault_session_id, dict):
            data = vault_session_id
        else:
            data = await self.load(int(vault_session_id))
            self._browser._vault_session_id = int(vault_session_id)

        profile = minimal_vault_profile(data)
        fingerprint = data.get("fingerprint")
        if isinstance(fingerprint, dict) and not fingerprint:
            fingerprint = None

        payload: dict[str, Any] = {"profile": profile}
        if isinstance(fingerprint, dict) and fingerprint:
            payload["fingerprint"] = fingerprint
        await self._browser.configure(**payload)

"""Automated captcha solving via a third-party service (2captcha).

This is the *automatic* counterpart to the human-action captcha flow in
:mod:`ceki_sdk._captcha`. It is opt-in: an agent sets ``CEKI_2CAPTCHA_API_KEY``
in its environment and calls :meth:`Browser.solve_captcha`, which finds an
hCaptcha / reCAPTCHA widget on the current page, submits it to the solving
service, and injects the returned token back into the page.

The API key lives only on the agent side (env) — it is never shipped into the
browser extension or any other component. The extension stays a dumb page;
the agent's own SDK does the solving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from ._browser import Browser

from ._exceptions import CaptchaError

log = logging.getLogger(__name__)

# 2captcha API endpoints (public, documented).
CREATE_TASK_URL = "https://api.2captcha.com/createTask"
GET_TASK_URL = "https://api.2captcha.com/getTaskResult"

# 2captcha task types we can solve.
CaptchaType = Literal["HCaptchaTaskProxyless", "RecaptchaV3TaskProxyless"]

# Default poll budget (seconds) for a single task. A normal hCaptcha solves in
# ~10-40s; give it a generous cap but fail fast rather than hang the agent.
DEFAULT_POLL_TIMEOUT = 120.0
DEFAULT_POLL_INTERVAL = 5.0

# Retry the whole createTask on these transient errors (empty balance etc. is
# fatal and raised immediately instead).
_RETRYABLE_TASK_ERRORS = {"ERROR_CAPTCHA_UNSOLVABLE"}


def _api_key_from_env() -> str:
    """Return the 2captcha key from env, or ``""`` when not configured.

    ``CEKI_2CAPTCHA_API_KEY`` is the canonical name; ``TWOCAPTCHA_API_KEY`` is
    accepted as a legacy alias (used by the earlier yandex-registration script).
    """
    return os.environ.get("CEKI_2CAPTCHA_API_KEY") or os.environ.get(
        "TWOCAPTCHA_API_KEY", ""
    ).strip()


@dataclass
class SolvedCaptcha:
    """The result of an automatically-solved captcha.

    ``solved`` is ``True`` when the token was retrieved and injected into the
    page; ``False`` otherwise (with ``error`` describing why). ``token`` is the
    raw solver response for hCaptcha / reCAPTCHA; ``task_type`` tells which
    widget was solved. ``elapsed`` is the total wall-clock time of the attempt.
    """

    solved: bool
    task_type: str = ""
    token: str = ""
    error: str = ""
    elapsed: float = 0.0
    #: Exact JSON payload the solver service returned for the final task.
    solver_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "solved": self.solved,
            "task_type": self.task_type,
            "token": self.token,
            "error": self.error,
            "elapsed": self.elapsed,
        }


class CaptchaSolver:
    """Thin async client around the 2captcha ``createTask``/``getTaskResult`` API.

    Stateless apart from the API key; safe to reuse across rentals.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        create_url: str = CREATE_TASK_URL,
        get_url: str = GET_TASK_URL,
        poll_timeout: float = DEFAULT_POLL_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._api_key = (api_key or _api_key_from_env()).strip()
        if not self._api_key:
            raise CaptchaError(
                "no captcha API key — set CEKI_2CAPTCHA_API_KEY (or "
                "TWOCAPTCHA_API_KEY) in the environment"
            )
        self._create_url = create_url
        self._get_url = get_url
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval

    async def _create_task(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> str:
        body = {"clientKey": self._api_key, "task": payload}
        resp = await client.post(self._create_url, json=body)
        if resp.status_code >= 400:
            raise CaptchaError(f"2captcha createTask HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        err = data.get("error", "")
        if err:
            desc = data.get("error_description", "")[:300]
            raise CaptchaError(f"2captcha createTask error {err}: {desc}")
        task_id = data.get("taskId")
        if not task_id:
            raise CaptchaError(f"2captcha createTask returned no taskId: {data}")
        return str(task_id)

    async def _get_task_result(self, client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
        body = {"clientKey": self._api_key, "taskId": task_id}
        resp = await client.post(self._get_url, json=body)
        if resp.status_code >= 400:
            raise CaptchaError(f"2captcha getTaskResult HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def solve(
        self,
        *,
        task_type: CaptchaType,
        website_url: str,
        website_key: str,
        extra: dict[str, Any] | None = None,
    ) -> SolvedCaptcha:
        """Create a solving task and poll until the token is ready.

        Returns ``SolvedCaptcha(solved=True)`` with the token, or a failed
        ``SolvedCaptcha`` (never raises for a solve that merely times out).
        A hard API error (bad key, empty balance) raises :class:`CaptchaError`.
        """
        started = time.monotonic()
        payload: dict[str, Any] = {
            "type": task_type,
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        if extra:
            payload.update(extra)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            task_id = await self._create_task(client, payload)

            deadline = time.monotonic() + self._poll_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return SolvedCaptcha(
                        solved=False,
                        task_type=task_type,
                        error="2captcha poll timeout",
                        elapsed=time.monotonic() - started,
                    )
                data = await self._get_task_result(client, task_id)
                status = data.get("status", "")
                if status == "ready":
                    solution = data.get("solution") or {}
                    token = solution.get("gRecaptchaResponse") or solution.get("token") or ""
                    if not token:
                        return SolvedCaptcha(
                            solved=False,
                            task_type=task_type,
                            error=f"2captcha ready but no token in solution: {solution}",
                            elapsed=time.monotonic() - started,
                            solver_response=data,
                        )
                    return SolvedCaptcha(
                        solved=True,
                        task_type=task_type,
                        token=token,
                        elapsed=time.monotonic() - started,
                        solver_response=data,
                    )
                err = data.get("error", "")
                if err:
                    if err in _RETRYABLE_TASK_ERRORS:
                        # Try a fresh task once; the previous attempt was
                        # unsolvable by the workers, not a config problem.
                        log.warning("2captcha %s, retrying once", err)
                        task_id = await self._create_task(client, payload)
                        deadline = time.monotonic() + self._poll_timeout
                        continue
                    return SolvedCaptcha(
                        solved=False,
                        task_type=task_type,
                        error=f"2captcha error {err}: {data.get('error_description', '')[:200]}",
                        elapsed=time.monotonic() - started,
                        solver_response=data,
                    )
                # status == "processing" (or anything else) — keep polling.
                await asyncio.sleep(self._poll_interval)


# ──────────────────────────────────────────────────────────────────────────
# DOM helpers — run inside the rented browser via Runtime.evaluate.
# ──────────────────────────────────────────────────────────────────────────

# Detect an hCaptcha / reCAPTCHA widget and return its type + site key, or
# {"found": false}. Returns JSON so the Python side can parse it directly.
_DETECT_JS = r"""
(() => {
  try {
    // hCaptcha: container div.h-captcha carries data-sitekey; the iframe
    // src also embeds it as /.../<sitekey>.
    const hc = document.querySelector('.h-captcha[data-sitekey], [data-hcaptcha-widget-id]');
    if (hc && hc.getAttribute('data-sitekey')) {
      return JSON.stringify({found: true, type: 'hcaptcha', key: hc.getAttribute('data-sitekey')});
    }
    const hcIframe = document.querySelector('iframe[src*="hcaptcha.com"]');
    if (hcIframe) {
      const m = (hcIframe.src || '').match(/hcaptcha\.com\/[^/]+\/([^/?#]+)/);
      if (m) return JSON.stringify({found: true, type: 'hcaptcha', key: m[1]});
    }
    // reCAPTCHA v2/v3: the g-recaptcha container or an anchor iframe.
    const rc = document.querySelector('.g-recaptcha[data-sitekey]');
    if (rc) {
      return JSON.stringify({found: true, type: 'recaptcha',
        key: rc.getAttribute('data-sitekey')});
    }
    const rcIframe = document.querySelector('iframe[src*="recaptcha/api2/anchor"]');
    if (rcIframe) {
      const m = (rcIframe.src || '').match(/[?&]k=([^&]+)/);
      if (m) return JSON.stringify({found: true, type: 'recaptcha', key: decodeURIComponent(m[1])});
    }
    return JSON.stringify({found: false});
  } catch (e) {
    return JSON.stringify({found: false, error: String(e)});
  }
})()
"""


def _js_str(value: str) -> str:
    """Escape a Python string for embedding inside a JS string literal."""
    return json.dumps(value)


# hCaptcha: set the hidden textarea and dispatch the callback hCaptcha
# registered (window.hcaptcha is the top-level API when loaded on the page).
# Fall back to submitting the surrounding form.
_INJECT_HCAPTCHA_JS = r"""
(token) => {
  try {
    const ta = document.querySelector('textarea[name="h-captcha-response"]');
    if (ta) {
      const proto = HTMLTextAreaElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      desc.set.call(ta, token);
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.dispatchEvent(new Event('change', {bubbles: true}));
    }
    let injected = false;
    if (typeof window.hcaptcha !== 'undefined' && window.hcaptcha.getResponse) {
      // Directly drive the widget's internal state via the public callback.
      try {
        window.hcaptcha.getResponse(); // no-op sanity
      } catch (e) { /* ignore */ }
    }
    // Trigger the form submit path: click the submit button if present.
    const form = ta && ta.closest('form');
    if (form) {
      const btn = form.querySelector('button[type="submit"], input[type="submit"]');
      if (btn) { btn.click(); injected = true; }
    }
    return JSON.stringify({injected, hasResponse: !!(ta && ta.value)});
  } catch (e) {
    return JSON.stringify({injected: false, error: String(e)});
  }
}
"""

# reCAPTCHA: drive grecaptcha.execute (v2 invisible / v3) with the token.
_INJECT_RECAPTCHA_JS = r"""
(token) => {
  try {
    let injected = false;
    if (typeof window.grecaptcha !== 'undefined') {
      // v3 / invisible: the token is normally passed to execute then the
      // callback fires with a new token; here we just record it and submit.
      const form = document.querySelector('.g-recaptcha')?.closest('form');
      if (form) {
        const btn = form.querySelector('button[type="submit"], input[type="submit"]');
        if (btn) { btn.click(); injected = true; }
      }
      // Also write the token into any hidden field expecting it.
      const hidden = document.querySelector('input[name="g-recaptcha-response"]');
      if (hidden) {
        const proto = HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        desc.set.call(hidden, token);
        hidden.dispatchEvent(new Event('input', {bubbles: true}));
        hidden.dispatchEvent(new Event('change', {bubbles: true}));
      }
      injected = injected || !!hidden;
    }
    return JSON.stringify({injected, hasResponse: true});
  } catch (e) {
    return JSON.stringify({injected: false, error: String(e)});
  }
}
"""


async def detect_captcha(browser: "Browser") -> dict[str, Any]:
    """Probe the current page for an hCaptcha/reCAPTCHA widget.

    Returns ``{"found": False}`` or ``{"found": True, "type": ..., "key": ...}``.
    """
    resp = await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": _DETECT_JS, "returnByValue": True},
    })
    value = resp.get("result", {}).get("value", "")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"found": False, "error": value}
    if isinstance(value, dict):
        return value
    return {"found": False}


async def inject_solution(browser: "Browser", task_type: str, token: str) -> dict[str, Any]:
    """Inject a solved token into the page's captcha widget.

    Returns the JSON payload reported by the injected JS (``{"injected": bool,
    ...}``). For hCaptcha it writes the hidden textarea and submits the form;
    for reCAPTCHA it drives ``grecaptcha``/hidden field and submits.
    """
    js = _INJECT_HCAPTCHA_JS if task_type.startswith("H") else _INJECT_RECAPTCHA_JS
    expr = f"({js})({_js_str(token)})"
    resp = await browser.send({
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True},
    })
    value = resp.get("result", {}).get("value", "")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"injected": False, "error": value}
    if isinstance(value, dict):
        return value
    return {"injected": False, "error": f"unexpected evaluate result: {value!r}"}

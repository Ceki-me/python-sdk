from __future__ import annotations

import asyncio
import email
import imaplib
import os
import re

CONFIRM_PATTERNS = {
    "reddit": re.compile(r"https://www\.reddit\.com/account/verify-email/[A-Za-z0-9_\-]+"),
    "github": re.compile(
        r"https://github\.com/users/[A-Za-z0-9_\-]+/email/verify\?[^\"\s]+"
    ),
}


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/html", "text/plain"):
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def _check_imap(tag: str, service: str) -> str | None:
    pattern = CONFIRM_PATTERNS[service]
    with imaplib.IMAP4_SSL(os.environ["IMAP_HOST"]) as m:
        m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASS"])
        m.select("INBOX")
        typ, data = m.search(None, f'TO "kom+{tag}@ceki.me"')
        if typ != "OK" or not data[0]:
            return None
        for msg_id in reversed(data[0].split()):
            typ2, msg_data = m.fetch(msg_id, "(RFC822)")
            if typ2 != "OK":
                continue
            raw = msg_data[0][1]  # type: ignore[index]
            msg = email.message_from_bytes(raw)
            body = _extract_body(msg)
            match = pattern.search(body)
            if match:
                return match.group(0)
    return None


async def wait_for_confirm_link(
    tag: str,
    *,
    timeout: float = 120,
    service: str = "reddit",
    poll_interval: float = 5,
) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        link = await asyncio.to_thread(_check_imap, tag, service)
        if link:
            return link
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"No confirm link for tag={tag} within {timeout}s")

"""Gmail API client — pulls + parses + marks-as-read messages via OAuth.

Auth: Google's OAuth refresh-token flow.
  1. Ben sets up an OAuth client + generates a refresh token (one-time, see
     README.md#gmail-oauth).
  2. This module exchanges the refresh token for a fresh access token at
     module-call time — access tokens are 1h-TTL so we don't bother caching
     across runs (and a long-running loop refreshes every iteration anyway).
  3. The access token rides in `Authorization: Bearer <token>` headers.

No google-auth or googleapiclient dependency — straight httpx + JSON. Keeps
the dep tree thin (the rest of pa-agent is already httpx + asyncpg).
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from pa_agent.settings import settings

log = logging.getLogger(__name__)

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(Exception):
    """Raised on any non-2xx Gmail API response."""


@dataclass(frozen=True)
class GmailMessage:
    """Parsed message — what the inbox loop hands to the triage LLM."""

    id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    body_text: str
    received_unix: int  # seconds since epoch (Gmail's `internalDate` / 1000)


def _credentials_present() -> bool:
    """Inbox loop calls this before each tick — keeps the loop dormant
    until Ben provisions all three OAuth pieces."""
    return bool(
        settings.gmail_oauth_client_id
        and settings.gmail_oauth_client_secret
        and settings.gmail_oauth_refresh_token
    )


async def _fetch_access_token(http: httpx.AsyncClient) -> str:
    """Exchange refresh_token → access_token. Raises GmailError on failure."""
    resp = await http.post(
        _OAUTH_TOKEN_URL,
        data={
            "client_id": settings.gmail_oauth_client_id,
            "client_secret": settings.gmail_oauth_client_secret,
            "refresh_token": settings.gmail_oauth_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10.0,
    )
    if resp.status_code >= 400:
        raise GmailError(
            f"oauth refresh failed: status={resp.status_code} body={resp.text[:200]}"
        )
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise GmailError(f"oauth response missing access_token: {body!r}")
    return token


def parse_message_payload(payload: dict[str, Any]) -> tuple[str, str]:
    """Pure helper — extract plaintext body + subject from Gmail's MIME-tree.

    Gmail's `messages.get` returns a recursive `payload` with `parts`. We
    walk it depth-first looking for the first text/plain part. If only HTML
    is present we strip tags as a last resort (good enough for triage). The
    full MIME tree gets dropped on the floor — the LLM doesn't need it.

    Returns (subject, body_text).
    """
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
    subject = headers.get("subject", "(no subject)")
    body_text = _walk_payload_for_text(payload) or payload.get("snippet", "")
    return subject, body_text


def _walk_payload_for_text(payload: dict[str, Any]) -> str | None:
    """Depth-first search for a text/plain (preferred) or text/html (fallback)
    body. Returns decoded UTF-8 string or None if no text found."""
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")

    if data and mime in ("text/plain", "text/html"):
        decoded = _decode_b64url(data)
        if mime == "text/html":
            decoded = _strip_html(decoded)
        return decoded

    for part in payload.get("parts") or []:
        found = _walk_payload_for_text(part)
        if found:
            return found
    return None


def _decode_b64url(s: str) -> str:
    """Gmail uses URL-safe base64 with optional padding stripped."""
    pad = "=" * ((4 - len(s) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((s + pad).encode("ascii")).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return ""


_HTML_TAG_RE = None


def _strip_html(html: str) -> str:
    """Crude HTML→text. Triage prompts are robust to noise so a regex strip
    is fine — adding bs4 to deps for one fallback path isn't worth it."""
    import re
    global _HTML_TAG_RE  # noqa: PLW0603
    if _HTML_TAG_RE is None:
        _HTML_TAG_RE = re.compile(r"<[^>]+>")
    return _HTML_TAG_RE.sub("", html).strip()


class GmailClient:
    """Thin async client around the few Gmail endpoints the inbox loop uses."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("GmailClient not started — call start() first")
        return self._http

    async def _bearer(self) -> dict[str, str]:
        token = await _fetch_access_token(self.http)
        return {"Authorization": f"Bearer {token}"}

    async def list_message_ids(
        self, query: str | None = None, max_results: int = 10
    ) -> list[str]:
        """Search by Gmail query (e.g. 'is:unread in:inbox'). Returns message IDs."""
        q = query or settings.gmail_query
        headers = await self._bearer()
        resp = await self.http.get(
            f"{_GMAIL_API}/messages",
            headers=headers,
            params={"q": q, "maxResults": max_results},
        )
        if resp.status_code >= 400:
            raise GmailError(f"list_messages failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        return [m["id"] for m in body.get("messages", [])]

    async def fetch_message(self, msg_id: str) -> GmailMessage:
        """Get one message in `full` format and parse to GmailMessage."""
        headers = await self._bearer()
        resp = await self.http.get(
            f"{_GMAIL_API}/messages/{msg_id}",
            headers=headers,
            params={"format": "full"},
        )
        if resp.status_code >= 400:
            raise GmailError(
                f"fetch_message {msg_id} failed: {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        payload = body.get("payload") or {}
        headers_map = {
            h["name"].lower(): h["value"] for h in payload.get("headers") or []
        }
        subject, body_text = parse_message_payload(payload)
        return GmailMessage(
            id=body.get("id", msg_id),
            thread_id=body.get("threadId", ""),
            sender=headers_map.get("from", "(unknown)"),
            subject=subject,
            snippet=body.get("snippet", ""),
            body_text=body_text,
            received_unix=int(body.get("internalDate", 0)) // 1000,
        )

    async def mark_as_read(self, msg_id: str) -> None:
        """Remove the UNREAD label so the next list call doesn't re-pull it."""
        headers = await self._bearer()
        resp = await self.http.post(
            f"{_GMAIL_API}/messages/{msg_id}/modify",
            headers=headers,
            json={"removeLabelIds": ["UNREAD"]},
        )
        if resp.status_code >= 400:
            raise GmailError(
                f"mark_as_read {msg_id} failed: {resp.status_code} {resp.text[:200]}"
            )


# Singleton — the inbox loop owns its own start/close lifecycle.
gmail_client = GmailClient()

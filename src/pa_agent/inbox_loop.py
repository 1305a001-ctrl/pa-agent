"""Background Gmail-pull loop.

Every `gmail_poll_interval_sec`, if OAuth is configured:
  1. List unread INBOX messages (capped per tick).
  2. For each: fetch full body, run through the existing triage_inbox_text,
     append to _inbox/triage-YYYY-MM-DD.md, mark as read.
  3. Forward summary to Telegram if urgency >= configured threshold.

Loop stays dormant if any OAuth setting is empty — no Gmail traffic until
Ben provisions creds. This keeps the scaffold safe to ship without breaking
existing /inbox manual-forward behaviour.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pa_agent import alerts
from pa_agent.gmail import GmailError, GmailMessage, _credentials_present, gmail_client
from pa_agent.pa import append_triage, triage_inbox_text
from pa_agent.settings import settings

log = logging.getLogger(__name__)


# Map textual urgency to int — used to compare against configured threshold.
_URGENCY_LEVELS = {"low": 1, "medium": 2, "high": 3}


def _should_forward_to_telegram(triage_urgency: str) -> bool:
    """Pure helper: is this triage urgent enough to push to Telegram?"""
    threshold_name = settings.gmail_telegram_min_urgency.lower()
    threshold = _URGENCY_LEVELS.get(threshold_name, 3)
    actual = _URGENCY_LEVELS.get(triage_urgency.lower(), 1)
    return actual >= threshold


def format_triage_for_telegram(msg: GmailMessage, triage: dict[str, str]) -> str:
    """Pure formatter — what shows up in Telegram when a high-urgency mail
    lands. Plain HTML so it works with alerts.telegram() unchanged."""
    icon = {"high": "🚨", "medium": "🟡"}.get(triage.get("urgency", ""), "📨")
    lines = [
        f"{icon} <b>Inbox: {triage.get('urgency', '?').upper()}</b>",
        f"<b>From:</b> {_safe(msg.sender)}",
        f"<b>Subject:</b> {_safe(msg.subject)}",
        "",
        f"<i>{_safe(triage.get('summary', ''))}</i>",
    ]
    actions = triage.get("action_items", "").strip()
    if actions:
        lines.append("")
        lines.append("<b>Actions:</b>")
        lines.append(_safe(actions))
    return "\n".join(lines)


def _safe(s: str) -> str:
    """Escape Telegram HTML reserved chars."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _process_one_message(msg: GmailMessage) -> None:
    """End-to-end for a single message — triage, archive, optionally notify,
    mark-as-read. Each step independently exception-trapped so one bad
    message doesn't block the loop."""
    text = (
        f"From: {msg.sender}\n"
        f"Subject: {msg.subject}\n"
        f"Date: <epoch={msg.received_unix}>\n"
        "\n"
        f"{msg.body_text or msg.snippet}"
    )

    try:
        triage = await triage_inbox_text(text)
    except Exception:
        log.exception("inbox_loop.triage_failed", extra={"msg_id": msg.id})
        return

    cc_path = settings.commandcenter_path or None
    try:
        path = append_triage(Path(cc_path) if cc_path else None, text, triage)
        if path:
            log.info("inbox_loop.archived msg=%s path=%s", msg.id, path)
    except Exception:
        log.exception("inbox_loop.archive_failed", extra={"msg_id": msg.id})

    if _should_forward_to_telegram(triage.get("urgency", "")):
        try:
            await alerts.telegram(format_triage_for_telegram(msg, triage))
        except Exception:
            log.exception("inbox_loop.telegram_failed", extra={"msg_id": msg.id})

    try:
        await gmail_client.mark_as_read(msg.id)
    except GmailError:
        log.exception("inbox_loop.mark_read_failed", extra={"msg_id": msg.id})
    except Exception:
        log.exception("inbox_loop.mark_read_unexpected", extra={"msg_id": msg.id})


async def _tick() -> int:
    """Single iteration of the loop. Returns count of messages processed."""
    if not _credentials_present():
        return 0

    try:
        ids = await gmail_client.list_message_ids(
            max_results=settings.gmail_max_messages_per_tick,
        )
    except GmailError:
        log.exception("inbox_loop.list_failed")
        return 0

    if not ids:
        return 0

    log.info("inbox_loop.tick fetched=%d", len(ids))
    processed = 0
    for msg_id in ids:
        try:
            msg = await gmail_client.fetch_message(msg_id)
        except GmailError:
            log.exception("inbox_loop.fetch_failed", extra={"msg_id": msg_id})
            continue
        await _process_one_message(msg)
        processed += 1
    return processed


async def loop() -> None:
    """Public entrypoint — main.py adds this as a 5th concurrent task.

    Lifecycle: starts the Gmail HTTP client, polls forever, closes on
    cancellation. Sleeps `gmail_poll_interval_sec` between ticks; if creds
    aren't configured, ticks are no-ops (just sleep)."""
    log.info(
        "inbox_loop.starting interval=%ds dormant=%s",
        settings.gmail_poll_interval_sec,
        not _credentials_present(),
    )
    await gmail_client.start()
    try:
        while True:
            try:
                processed = await _tick()
                if processed > 0:
                    log.info("inbox_loop.tick_done processed=%d", processed)
            except Exception:
                log.exception("inbox_loop.tick_unexpected_error")
            await asyncio.sleep(settings.gmail_poll_interval_sec)
    finally:
        await gmail_client.close()

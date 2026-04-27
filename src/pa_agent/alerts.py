"""Telegram delivery + message formatting."""
import logging

import httpx

from pa_agent.models import Signal
from pa_agent.settings import settings

log = logging.getLogger(__name__)

MAX_TELEGRAM_CHARS = 4096  # hard cap per message


async def telegram(text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("Telegram skipped (no creds): %s", text[:80])
        return False
    if len(text) > MAX_TELEGRAM_CHARS:
        text = text[: MAX_TELEGRAM_CHARS - 100] + "\n…(truncated)"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            return r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram failed: %s", exc)
        return False


def format_critical(s: Signal) -> str:
    """Rich format for a signals:critical message."""
    direction_emoji = {"long": "📈", "short": "📉", "neutral": "➖", "watch": "👀"}.get(
        s.direction, "•"
    )
    risk = s.composite_risk_score or 0.0
    reasoning = ((s.payload or {}).get("reasoning") or "").strip()
    strategy_name = (s.payload or {}).get("strategy_name") or "?"

    lines = [
        f"<b>🚨 CRITICAL</b> {direction_emoji} {s.asset} {s.direction.upper()}",
        f"conf <b>{s.confidence:.2f}</b> · risk <b>{risk:.2f}</b>",
        f"<i>{strategy_name}</i>",
    ]
    if reasoning:
        lines.append("")
        lines.append(reasoning[:600])
    if s.source_article_ids:
        lines.append("")
        lines.append(f"<i>Based on {len(s.source_article_ids)} article(s)</i>")
    return "\n".join(lines)

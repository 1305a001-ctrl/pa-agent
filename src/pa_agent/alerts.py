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


def format_correlation_alert(payload: dict) -> str:
    """Pure: structured corr alert (from risk:correlation_alerts) → Telegram HTML.

    Payload shape (set by risk-watcher v0.9):
      {
        "ts": "2026-05-07T...",
        "transition": "cluster_forming" | "cluster_resolved",
        "max_corr": 0.92,
        "cluster_count": 1,
        "threshold": 0.85,
        "universe_size": 19,
        "top_pairs": [{"a":"BTC-USDT","b":"ETH-USDT","rho":0.95}, ...],
      }
    """
    transition = payload.get("transition", "unknown")
    icon = "⚠️" if transition == "cluster_forming" else "✅"
    headline = (
        "RISK CLUSTER FORMING" if transition == "cluster_forming"
        else "Cluster resolved"
    )
    max_corr = payload.get("max_corr")
    threshold = payload.get("threshold", 0.85)
    cluster_count = payload.get("cluster_count", 0)
    universe = payload.get("universe_size", 0)
    pairs = payload.get("top_pairs") or []

    lines = [
        f"<b>{icon} {headline}</b>",
        f"max ρ <b>{max_corr:.3f}</b> vs threshold {threshold:.2f}"
        if max_corr is not None
        else f"threshold {threshold:.2f}",
        f"clusters above threshold: <b>{cluster_count}</b> · universe size: {universe}",
    ]
    if pairs:
        lines.append("")
        lines.append("<i>Top pairs by |ρ|:</i>")
        for p in pairs[:5]:
            lines.append(f"  • {p['a']}↔{p['b']}: <code>{p['rho']:+.3f}</code>")
    return "\n".join(lines)


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

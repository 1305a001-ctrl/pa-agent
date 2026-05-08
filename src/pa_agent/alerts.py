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


def format_poly_settlement(payload: dict) -> str:
    """Pure: structured settlement event (from poly:settlement_alerts) →
    Telegram HTML.

    Payload shape (set by poly-adapter v0.2 settlement.py):
      {
        "event": "poly_position_settled",
        "position_id": "<uuid>",
        "slug": "bitcoin-up-or-down-on-may-6-2026",
        "side": "long" | "short",
        "qty": 4028.0,
        "entry_price": 0.50,
        "final_yes_price": 1.0,
        "yes_won": true | false,
        "realized_pnl_usd": 2014.0,
        "reason": "closed_yes" | "past_end_yes_decisive" | …,
        "settled_at": "2026-05-08T…",
      }
    """
    pnl = payload.get("realized_pnl_usd") or 0.0
    yes_won = payload.get("yes_won")
    side = payload.get("side", "?")
    slug = payload.get("slug", "?")
    qty = payload.get("qty") or 0.0
    entry = payload.get("entry_price") or 0.0
    final_yes = payload.get("final_yes_price") or 0.0
    reason = payload.get("reason", "?")

    win = pnl > 0
    icon = "🟢" if win else ("🔴" if pnl < 0 else "⚪")
    outcome = "YES won" if yes_won else "NO won" if yes_won is False else "?"

    sign = "+" if pnl > 0 else ("-" if pnl < 0 else "")
    pnl_abs_fmt = f"{abs(pnl):,.2f}"
    lines = [
        f"<b>{icon} Poly settled — {outcome}</b>",
        f"<code>{slug}</code>",
        f"{side} qty <b>{qty:.2f}</b> · entry {entry:.4f} → final {final_yes:.4f}",
        f"realized PnL: <b>{sign}${pnl_abs_fmt}</b>",
        f"<i>via {reason}</i>",
    ]
    return "\n".join(lines)


def format_alpha_fire(alpha: dict) -> str:
    """Pure: Polymarket alpha emission (from alphas:active) → Telegram HTML.

    Tracks per-strategy fires so Ben sees real-time activity from
    poly-fade-extreme / poly-liq-cascade-taker-daily / etc. without
    SSH-grepping the strategy-runners logs.
    """
    metadata = alpha.get("metadata") or {}
    direction = alpha.get("direction") or "?"
    confidence = float(alpha.get("confidence") or 0.0)
    strategy_slug = metadata.get("strategy_slug", "?")
    asset = metadata.get("asset") or alpha.get("asset", "?")
    cadence = metadata.get("cadence", "")
    edge_pp = metadata.get("edge_pp")
    edge_bps = alpha.get("edge_bps")
    yes_price = metadata.get("yes_price")
    cascade_total = metadata.get("cascade_total_usd")
    perp_return = metadata.get("perp_return") or metadata.get("perp_return_5m")

    direction_emoji = {"long": "📈", "short": "📉"}.get(direction, "•")
    side_label = (
        "buy YES" if direction == "long"
        else "buy NO" if direction == "short"
        else direction
    )

    # Strategy-specific context line — present only the fields each strategy
    # emits, so messages stay short and informative.
    context_lines: list[str] = []
    if yes_price is not None and perp_return is not None:
        # Fade-extreme / fade-daily — extreme + perp non-confirmation
        context_lines.append(
            f"YES <b>{float(yes_price):.3f}</b> · perp {float(perp_return):+.4f}"
        )
    elif cascade_total:
        # Liq-cascade-taker — triggered by liq cascade
        context_lines.append(
            f"liq cascade <b>${float(cascade_total):,.0f}</b> on "
            f"{metadata.get('trigger_strategy_slug', '?')}"
        )
    if edge_pp is not None:
        context_lines.append(f"edge <b>{float(edge_pp):.3f}pp</b>")
    elif edge_bps is not None:
        context_lines.append(f"edge <b>{int(edge_bps)} bps</b>")

    lines = [
        f"<b>{direction_emoji} {strategy_slug}</b> · {asset}{' '+cadence if cadence else ''}",
        f"<code>{alpha.get('asset', '?')}</code>",
        f"{side_label} · confidence <b>{confidence:.2f}</b>",
    ]
    if context_lines:
        lines.append(" · ".join(context_lines))
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

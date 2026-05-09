"""Per-strategy auto-disable monitor.

Watches each tracked strategy's recent closed positions; if consecutive
losses exceed a threshold OR 24h realized PnL is too negative, marks
the strategy as halted by adding it to a Redis set. Sends a Telegram
alert with diagnostic numbers.

The Redis halt set is the source of truth for runtime suppression —
alpha-fusion can read it and drop alphas from halted strategies. (That
enforcement layer is a follow-up; for now, the monitor + alert lets the
operator manually flip the strategy frontmatter to `inactive` if they
agree with the call.)

Pure helpers here are tested without DB; the loop itself is integration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DisableDecision:
    """Pure result: should we disable this strategy and why."""

    should_halt: bool
    reason: str
    consecutive_losses: int
    realized_24h: float
    n_closed_24h: int


def evaluate_strategy_halt(
    *,
    recent_closed_pnls: list[float],
    pnls_24h: list[float],
    consecutive_loss_threshold: int,
    realized_24h_threshold: float,
) -> DisableDecision:
    """Pure: decide whether a strategy should be auto-halted.

    `recent_closed_pnls` — newest-first list of realized PnLs on the last
    N closed positions. Counts consecutive losses from the front.

    `pnls_24h` — realized PnLs for closed positions in the last 24 hours.

    Halts when:
      - consecutive_losses >= consecutive_loss_threshold, OR
      - sum(pnls_24h) <= realized_24h_threshold (a negative number)

    Both thresholds need to be reasonably loose so that healthy strategies
    don't get tripped on noise.
    """
    consecutive = 0
    for pnl in recent_closed_pnls:
        if pnl < 0:
            consecutive += 1
        else:
            break

    realized_24h = sum(pnls_24h)
    n_closed_24h = len(pnls_24h)

    reasons: list[str] = []
    if consecutive >= consecutive_loss_threshold:
        reasons.append(
            f"{consecutive} consecutive losses (≥ {consecutive_loss_threshold})"
        )
    if realized_24h <= realized_24h_threshold:
        reasons.append(
            f"24h realized PnL ${realized_24h:+.2f} ≤ ${realized_24h_threshold:+.2f}"
        )

    return DisableDecision(
        should_halt=bool(reasons),
        reason="; ".join(reasons) if reasons else "ok",
        consecutive_losses=consecutive,
        realized_24h=realized_24h,
        n_closed_24h=n_closed_24h,
    )


def format_halt_alert(strategy_slug: str, decision: DisableDecision) -> str:
    """Pure: HTML-formatted Telegram alert for a halt event."""
    return (
        f"<b>🛑 Auto-halt: {strategy_slug}</b>\n"
        f"<i>{decision.reason}</i>\n\n"
        f"  consecutive losses: <b>{decision.consecutive_losses}</b>\n"
        f"  24h realized PnL: <b>${decision.realized_24h:+.2f}</b> across "
        f"{decision.n_closed_24h} closed\n\n"
        f"<i>Strategy added to <code>strategy:halts</code> Redis set.</i>\n"
        f"<i>Manual override: SREM <code>strategy:halts</code> {strategy_slug}.</i>\n"
        f"<i>Permanent: flip frontmatter status: inactive in strategy-library.</i>"
    )


def parse_pnls_for_24h(records: list[Any], *, now: datetime) -> list[float]:
    """Pure: filter closed-position records to last 24 hours, return realized PnLs.

    `records` are asyncpg.Record-like objects (or dicts) with at least
    `closed_at` (datetime) and `realized_pnl_usd` (float). Records with
    NULL closed_at or NULL pnl are skipped.
    """
    cutoff = now - timedelta(hours=24)
    out: list[float] = []
    for r in records:
        closed_at = r["closed_at"] if isinstance(r, dict) else getattr(r, "closed_at", None)
        pnl = r["realized_pnl_usd"] if isinstance(r, dict) else getattr(r, "realized_pnl_usd", None)
        if closed_at is None or pnl is None:
            continue
        if closed_at < cutoff:
            continue
        try:
            out.append(float(pnl))
        except (TypeError, ValueError):
            continue
    return out


def now_utc() -> datetime:
    """Single point of `datetime.now(UTC)` for testability."""
    return datetime.now(UTC)

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
    # Proactive warning when we're one trade away from halt — gives operator
    # a chance to intervene before the safety net triggers. Added 2026-05-19
    # post-mortem after a 6-loss streak ran to completion before halt fired.
    should_warn: bool = False
    warn_reason: str = ""


def evaluate_strategy_halt(
    *,
    recent_closed_pnls: list[float],
    pnls_24h: list[float],
    consecutive_loss_threshold: int,
    realized_24h_threshold: float,
) -> DisableDecision:
    """Pure: decide whether a strategy should be auto-halted, OR warned.

    `recent_closed_pnls` — newest-first list of realized PnLs on the last
    N closed positions. Counts consecutive losses from the front.

    `pnls_24h` — realized PnLs for closed positions in the last 24 hours.

    Halts when:
      - consecutive_losses >= consecutive_loss_threshold, OR
      - sum(pnls_24h) <= realized_24h_threshold (a negative number)

    Warns (without halting) when:
      - consecutive_losses == consecutive_loss_threshold - 1
        (one short of halt — proactive notice so operator can intervene)
      - sum(pnls_24h) is between 60-100% of the realized_24h_threshold
        (approaching the 24h damage cap)

    Both thresholds need to be reasonably loose so that healthy strategies
    don't get tripped on noise; the warn level is one step tighter.
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
    should_halt = bool(reasons)

    # Proactive warning: one short of halt threshold OR 60-100% of 24h damage cap.
    warn_reasons: list[str] = []
    if not should_halt:
        if consecutive_loss_threshold >= 2 and consecutive == consecutive_loss_threshold - 1:
            warn_reasons.append(
                f"{consecutive} consecutive losses — one more triggers halt"
            )
        # 24h drawdown approaching cap (60-100% of threshold)
        if realized_24h_threshold < 0:
            # threshold is negative (e.g. -100); compute fraction toward cap
            frac = realized_24h / realized_24h_threshold  # both negative → positive
            if 0.6 <= frac < 1.0:
                warn_reasons.append(
                    f"24h drawdown ${realized_24h:+.2f} at {frac*100:.0f}% of cap "
                    f"${realized_24h_threshold:+.2f}"
                )

    return DisableDecision(
        should_halt=should_halt,
        reason="; ".join(reasons) if reasons else "ok",
        consecutive_losses=consecutive,
        realized_24h=realized_24h,
        n_closed_24h=n_closed_24h,
        should_warn=bool(warn_reasons),
        warn_reason="; ".join(warn_reasons),
    )


def format_halt_alert(strategy_slug: str, decision: DisableDecision) -> str:
    """Pure: HTML-formatted Telegram alert for a halt event."""
    return (
        f"<b>🛑 Auto-halt: {strategy_slug}</b>\n"
        f"<i>{decision.reason}</i>\n\n"
        f"  consecutive losses: <b>{decision.consecutive_losses}</b>\n"
        f"  24h realized PnL: <b>${decision.realized_24h:+.2f}</b> across "
        f"{decision.n_closed_24h} closed\n\n"
        f"<i>Strategy added to <code>strategy:halts</code> Redis set "
        f"AND <code>system:halt:strategy:{strategy_slug}</code> key (oms-gateway).</i>\n"
        f"<i>Manual override: SREM <code>strategy:halts</code> {strategy_slug} "
        f"AND DEL <code>system:halt:strategy:{strategy_slug}</code>.</i>\n"
        f"<i>Permanent: flip frontmatter status: inactive in strategy-library.</i>"
    )


def format_streak_warning(strategy_slug: str, decision: DisableDecision) -> str:
    """Pure: HTML-formatted Telegram alert for a near-halt streak warning.

    Fires when we're one trade short of halt or approaching the 24h cap.
    Does NOT halt — gives operator a chance to intervene before the
    safety net trips.
    """
    return (
        f"<b>⚠️ Streak warning: {strategy_slug}</b>\n"
        f"<i>{decision.warn_reason}</i>\n\n"
        f"  consecutive losses: <b>{decision.consecutive_losses}</b>\n"
        f"  24h realized PnL: <b>${decision.realized_24h:+.2f}</b> across "
        f"{decision.n_closed_24h} closed\n\n"
        f"<i>Not yet halted. One more loss / further drawdown trips auto-halt.</i>"
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

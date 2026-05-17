"""Position-aging monitor: warn the operator about stale opens.

Different strategy buckets have different intended hold times — a
fast-intraday signal sitting open for three days is a stuck position;
a `conviction` swing position sitting for the same three days is fine.
This module turns open-position rows into a small set of alert records,
one per (position, age-tier) crossing, so the operator gets exactly one
ping per stale position per tier instead of a re-alert on every cycle.

Polymarket positions are excluded — they're naturally bounded by market
resolution, and the settlement loop already alerts when those close.

Pure helpers tested without DB; the loop in main.py is integration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


# Per-bucket age tiers (hours). Each tier fires one alert when crossed.
# Buckets not in this dict (or `None` from missing frontmatter) get the
# default tiers — operator can review later via /status.
BUCKET_AGE_TIERS_HOURS: dict[str, tuple[float, ...]] = {
    "fast-intraday": (6.0, 24.0, 72.0),
    "swing":        (72.0, 168.0, 336.0),     # 3d / 7d / 14d
    "conviction":   (336.0, 720.0, 1440.0),    # 14d / 30d / 60d
    "hedge":        (168.0, 336.0, 720.0),     # 7d / 14d / 30d
}
DEFAULT_AGE_TIERS_HOURS: tuple[float, ...] = (24.0, 168.0, 720.0)

# Polymarket positions skip aging — they're resolution-bound.
SKIP_VENUES: frozenset[str] = frozenset({"polymarket"})


@dataclass(frozen=True)
class AgingAlert:
    """One alert event: (position, tier) just crossed."""
    position_id: str
    slug: str
    bucket: str | None
    asset: str
    venue: str
    age_hours: float
    tier_hours: float
    unrealized_pnl_usd: float | None
    alert_key: str  # for de-dup against Redis sent-set


def _tiers_for(bucket: str | None) -> tuple[float, ...]:
    return BUCKET_AGE_TIERS_HOURS.get(bucket or "", DEFAULT_AGE_TIERS_HOURS)


def evaluate_aging(
    rows: list[Any],
    *,
    now: datetime,
    already_alerted: set[str],
) -> list[AgingAlert]:
    """Pure: compute new aging alerts for open positions.

    `rows` are dict-like records from db.open_positions_for_aging().
    `already_alerted` is the Redis set of (position_id, tier_hours) keys
    that have been sent — same key twice ⇒ no duplicate alert.

    Returns the highest-tier crossing per position so a position that
    just blew past 24h *and* 6h yields one alert at 24h, not two.
    """
    out: list[AgingAlert] = []
    for r in rows:
        venue = (r["venue"] if isinstance(r, dict) else getattr(r, "venue", "")) or ""
        if venue.lower() in SKIP_VENUES:
            continue

        opened_at = r["opened_at"] if isinstance(r, dict) else getattr(r, "opened_at", None)
        if opened_at is None:
            continue
        age_hours = (now - opened_at).total_seconds() / 3600.0
        if age_hours <= 0:
            continue

        bucket = r["bucket"] if isinstance(r, dict) else getattr(r, "bucket", None)
        tiers = _tiers_for(bucket)

        # Find the highest-tier we've crossed but haven't alerted on yet.
        position_id = str(
            r["position_id"] if isinstance(r, dict) else getattr(r, "position_id", "?")
        )
        crossed: float | None = None
        for tier in tiers:
            if age_hours < tier:
                continue
            key = f"{position_id}:{tier:.0f}"
            if key in already_alerted:
                continue
            crossed = tier  # keep updating to find the highest unalerted tier

        if crossed is None:
            continue

        slug = r["slug"] if isinstance(r, dict) else getattr(r, "slug", "?")
        asset = r["asset"] if isinstance(r, dict) else getattr(r, "asset", "?")
        upnl = (
            r["unrealized_pnl_usd"]
            if isinstance(r, dict)
            else getattr(r, "unrealized_pnl_usd", None)
        )
        out.append(
            AgingAlert(
                position_id=position_id,
                slug=str(slug),
                bucket=bucket if bucket is None else str(bucket),
                asset=str(asset),
                venue=venue,
                age_hours=round(age_hours, 1),
                tier_hours=crossed,
                unrealized_pnl_usd=float(upnl) if upnl is not None else None,
                alert_key=f"{position_id}:{crossed:.0f}",
            )
        )
    return out


def format_aging_alert(alerts: list[AgingAlert]) -> str:
    """Pure: HTML Telegram body for a batch of aging alerts.

    One message lists every newly-stale position so the operator
    gets a single ping per cycle even when many cross thresholds.
    """
    if not alerts:
        return ""
    lines = [
        f"<b>⏳ Stale open positions — {len(alerts)} crossed an age tier</b>",
        "",
    ]
    for a in alerts:
        upnl_str = (
            f"{a.unrealized_pnl_usd:+.2f}"
            if a.unrealized_pnl_usd is not None
            else "—"
        )
        bucket_label = a.bucket or "(no-bucket)"
        lines.append(
            f"<code>{a.slug:<28}</code> {a.venue} {a.asset} · "
            f"age <b>{_fmt_age(a.age_hours)}</b> ≥ {_fmt_age(a.tier_hours)} "
            f"({bucket_label}) · uPnL ${upnl_str}"
        )
    lines.append("")
    lines.append(
        "<i>Review with /status, or close manually if the thesis has expired.</i>"
    )
    return "\n".join(lines)


def _fmt_age(hours: float) -> str:
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24.0
    return f"{days:.1f}d"


def now_utc() -> datetime:
    from datetime import UTC
    return datetime.now(UTC)


__all__ = [
    "AgingAlert",
    "BUCKET_AGE_TIERS_HOURS",
    "DEFAULT_AGE_TIERS_HOURS",
    "SKIP_VENUES",
    "evaluate_aging",
    "format_aging_alert",
    "now_utc",
    "timedelta",
]

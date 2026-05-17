"""Daily monitoring + real-time alerts for the new edge features.

Four loops + a digest section, all extending the existing pa-agent
pattern (XREAD stream / poll Redis key → format → telegram.send).

  decay_halt_alert_loop   subscribe `risk:strategy_halt_events`,
                          surface auto-halts when decay detector trips
  kelly_outlier_loop      poll `oms:kelly:allocations` every 10min,
                          alert when a strategy's multiplier crosses
                          2.0+ (top performer) or 0.3- (decay candidate)
  bankroll_tier_loop      poll `oms:bankroll:state`, alert when the
                          active tier changes (T1→T2, T2→T1, etc)
  oracle_latency_loop     poll `oracle:latency:summary:<asset>`,
                          alert when p50 lead drops below 1.0s
                          (edge is shrinking — review GMX/chainlink-lag)

Each loop fail-OPENs: a transient Redis hiccup doesn't kill the loop;
backoff + retry next cycle. All formatters are pure for unit testing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from pa_agent import alerts
from pa_agent.settings import settings

log = logging.getLogger(__name__)


# Redis keys / streams (must match producers)
HALT_EVENTS_STREAM = "risk:strategy_halt_events"
KELLY_STATE_KEY = "oms:kelly:allocations"
BANKROLL_STATE_KEY = "oms:bankroll:state"
ORACLE_LATENCY_KEY_PATTERN = "oracle:latency:summary:{asset}"

# Per-loop config (defaults; operator can override via env)
KELLY_POLL_INTERVAL_SEC = 600              # 10 min
BANKROLL_POLL_INTERVAL_SEC = 300           # 5 min
ORACLE_LATENCY_POLL_INTERVAL_SEC = 600     # 10 min

# Alert thresholds
KELLY_TOP_PERFORMER_THRESHOLD = 2.0    # mult ≥ 2.0 → notable winner
KELLY_DECAY_THRESHOLD = 0.3            # mult ≤ 0.3 → near floor; risk
ORACLE_LATENCY_DEGRADATION_SEC = 1.0   # p50 < 1s means edge is shrinking
ORACLE_LATENCY_ASSETS = ("btc", "eth", "sol", "wsteth", "cbeth")


# ─── Formatters (pure, testable) ──────────────────────────────────────


def format_decay_halt(event: dict[str, Any]) -> str:
    """Pure: render a decay-halt event as a Telegram message."""
    slug = str(event.get("slug") or "?")
    reason = str(event.get("reason") or "")
    ttl_h = int(event.get("ttl_sec", 0)) // 3600
    return (
        f"⚠️ *Strategy auto-halted*\n"
        f"slug: `{slug}`\n"
        f"reason: {reason}\n"
        f"halt duration: {ttl_h}h\n"
        f"Investigate in `decay:{slug}:state`, clear via `/resume {slug}`."
    )


def format_kelly_outlier(
    *, slug: str, multiplier: float, kind: str,
) -> str:
    """Pure: render a Kelly outlier alert. kind ∈ {'top', 'decay'}."""
    emoji = "🚀" if kind == "top" else "🐢"
    label = "TOP PERFORMER" if kind == "top" else "ALLOCATION FLOORED"
    return (
        f"{emoji} *Kelly {label}*\n"
        f"slug: `{slug}`\n"
        f"allocation multiplier: *{multiplier:.2f}x*\n"
        f"({'budget scaled up' if kind == 'top' else 'budget reduced — review edge'})"
    )


def format_bankroll_tier_transition(
    *, from_tier: str, to_tier: str, pnl_usd: float,
) -> str:
    """Pure: render a tier-up / tier-down message."""
    direction = "📈" if to_tier > from_tier else "📉"
    return (
        f"{direction} *Bankroll tier {to_tier}*\n"
        f"transition: {from_tier} → *{to_tier}*\n"
        f"realized pnl: *${pnl_usd:,.2f}*\n"
        f"trade caps adjusted automatically."
    )


def format_oracle_latency_degradation(
    *, asset: str, p50_lead_sec: float, p95_lead_sec: float,
) -> str:
    """Pure: render an oracle latency alert."""
    return (
        f"⏱ *Oracle latency degradation*\n"
        f"asset: *{asset.upper()}*\n"
        f"p50 lead: {p50_lead_sec:.2f}s\n"
        f"p95 lead: {p95_lead_sec:.2f}s\n"
        f"Edge window is shrinking — GMX/chainlink-lag capture may drop."
    )


def format_daily_digest(
    *,
    poly_pnl_24h: float,
    poly_closed_24h: int,
    poly_win_rate_24h: float,
    gmx_paper_pnl_24h: float,
    gmx_unique_whales_24h: int,
    aave_candidates_24h: int,
    bankroll_tier: str,
    bankroll_pnl_total: float,
    top_kelly: list[tuple[str, float]],
    halted_strategies: int,
) -> str:
    """Pure: build the end-of-day digest message."""
    lines = [
        "📊 *Daily Edge Digest*",
        "",
        "*Polymarket*",
        f"  24h PnL: *${poly_pnl_24h:,.2f}*",
        f"  closed: {poly_closed_24h} | win rate: {poly_win_rate_24h:.1f}%",
        "",
        "*GMX*",
        f"  paper PnL: *${gmx_paper_pnl_24h:,.0f}* (theoretical)",
        f"  unique whales tracked: {gmx_unique_whales_24h}",
        "",
        "*Aave V3*",
        f"  candidates today: {aave_candidates_24h}",
        "",
        "*Bankroll*",
        f"  active tier: *{bankroll_tier}*",
        f"  realized PnL total: *${bankroll_pnl_total:,.2f}*",
        "",
        "*Top Kelly Allocations*",
    ]
    for slug, mult in top_kelly[:5]:
        lines.append(f"  `{slug}` → {mult:.2f}x")
    lines += [
        "",
        f"*Auto-halted strategies*: {halted_strategies}",
    ]
    return "\n".join(lines)


# ─── Loops ────────────────────────────────────────────────────────────


async def decay_halt_alert_loop() -> None:
    """Subscribe `risk:strategy_halt_events` from $ — surface auto-halts."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    last_id = "$"
    backoff = 1.0
    log.info("decay-halt-alert loop starting; subscribing to %s",
             HALT_EVENTS_STREAM)
    while True:
        try:
            result = await r.xread(
                {HALT_EVENTS_STREAM: last_id},
                block=10_000,
                count=10,
            )
        except Exception:
            log.exception("decay-halt-alert XREAD failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        if not result:
            continue
        for _stream, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                if getattr(settings, "pa_agent_halt", False):
                    continue
                try:
                    payload = {k: v for k, v in fields.items()}
                    text = format_decay_halt(payload)
                    await alerts.telegram(text)
                    log.info(
                        "decay-halt-alert sent slug=%s",
                        payload.get("slug"),
                    )
                except Exception:
                    log.exception(
                        "decay-halt-alert process failed for %s", entry_id,
                    )


async def kelly_outlier_loop() -> None:
    """Poll oms:kelly:allocations; alert when a strategy's multiplier
    crosses the top/decay thresholds vs the prior poll."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    prior_multipliers: dict[str, float] = {}
    log.info("kelly-outlier loop starting; polling every %ds",
             KELLY_POLL_INTERVAL_SEC)
    while True:
        await asyncio.sleep(KELLY_POLL_INTERVAL_SEC)
        try:
            raw = await r.get(KELLY_STATE_KEY)
        except Exception:
            log.exception("kelly-outlier read failed")
            continue
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for slug, mult_raw in payload.items():
            if slug.startswith("__"):
                continue   # internal keys (e.g. refresh ts)
            try:
                mult = float(mult_raw)
            except (TypeError, ValueError):
                continue
            prior = prior_multipliers.get(slug)
            prior_multipliers[slug] = mult
            if prior is None:
                continue
            # Surface threshold crossings (prevents repeated alerts)
            if mult >= KELLY_TOP_PERFORMER_THRESHOLD > prior:
                try:
                    await alerts.telegram(
                        format_kelly_outlier(slug=slug, multiplier=mult, kind="top"),
                    )
                except Exception:
                    log.exception("kelly-outlier alert failed")
            elif mult <= KELLY_DECAY_THRESHOLD < prior:
                try:
                    await alerts.telegram(
                        format_kelly_outlier(slug=slug, multiplier=mult, kind="decay"),
                    )
                except Exception:
                    log.exception("kelly-outlier alert failed")


async def bankroll_tier_loop() -> None:
    """Poll oms:bankroll:state; alert on tier_label transitions."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    prior_tier: str | None = None
    log.info("bankroll-tier loop starting; polling every %ds",
             BANKROLL_POLL_INTERVAL_SEC)
    while True:
        await asyncio.sleep(BANKROLL_POLL_INTERVAL_SEC)
        try:
            raw = await r.get(BANKROLL_STATE_KEY)
        except Exception:
            log.exception("bankroll-tier read failed")
            continue
        if not raw:
            continue
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            continue
        tier = str(state.get("tier_label") or "")
        if not tier:
            continue
        try:
            pnl = float(state.get("realized_pnl_usd") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        if prior_tier is None:
            prior_tier = tier
            continue
        if tier != prior_tier:
            try:
                await alerts.telegram(format_bankroll_tier_transition(
                    from_tier=prior_tier, to_tier=tier, pnl_usd=pnl,
                ))
                log.info(
                    "bankroll-tier transition %s → %s pnl=%.2f",
                    prior_tier, tier, pnl,
                )
            except Exception:
                log.exception("bankroll-tier alert failed")
            prior_tier = tier


async def oracle_latency_loop() -> None:
    """Poll per-asset latency summaries; alert when p50 lead < 1.0s."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    last_alert_ts: dict[str, float] = {}
    log.info("oracle-latency loop starting; polling every %ds",
             ORACLE_LATENCY_POLL_INTERVAL_SEC)
    while True:
        await asyncio.sleep(ORACLE_LATENCY_POLL_INTERVAL_SEC)
        for asset in ORACLE_LATENCY_ASSETS:
            try:
                raw = await r.get(ORACLE_LATENCY_KEY_PATTERN.format(asset=asset))
            except Exception:
                continue
            if not raw:
                continue
            try:
                summary = json.loads(raw)
            except (TypeError, ValueError):
                continue
            try:
                p50 = float(summary.get("p50_lead_sec") or 0.0)
                p95 = float(summary.get("p95_lead_sec") or 0.0)
            except (TypeError, ValueError):
                continue
            if p50 < ORACLE_LATENCY_DEGRADATION_SEC:
                # Throttle: at most one alert per asset per 4h
                now = time.time()
                if now - last_alert_ts.get(asset, 0.0) < 4 * 3600:
                    continue
                last_alert_ts[asset] = now
                try:
                    await alerts.telegram(format_oracle_latency_degradation(
                        asset=asset, p50_lead_sec=p50, p95_lead_sec=p95,
                    ))
                    log.info(
                        "oracle-latency alert sent asset=%s p50=%.2f",
                        asset, p50,
                    )
                except Exception:
                    log.exception("oracle-latency alert failed")


__all__ = [
    "HALT_EVENTS_STREAM",
    "KELLY_STATE_KEY",
    "BANKROLL_STATE_KEY",
    "ORACLE_LATENCY_KEY_PATTERN",
    "KELLY_TOP_PERFORMER_THRESHOLD",
    "KELLY_DECAY_THRESHOLD",
    "ORACLE_LATENCY_DEGRADATION_SEC",
    "ORACLE_LATENCY_ASSETS",
    "format_decay_halt",
    "format_kelly_outlier",
    "format_bankroll_tier_transition",
    "format_oracle_latency_degradation",
    "format_daily_digest",
    "decay_halt_alert_loop",
    "kelly_outlier_loop",
    "bankroll_tier_loop",
    "oracle_latency_loop",
]

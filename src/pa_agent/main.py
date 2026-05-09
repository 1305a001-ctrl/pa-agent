"""pa-agent daemon — Phase 8 v0.7.

Six concurrent loops:
  1. critical_loop      — subscribes to signals:critical, sends rich Telegram alerts
  2. brief_loop         — fires once per day at BRIEF_LOCAL_HOUR
  3. bot_loop           — long-poll Telegram getUpdates for /ask /note /inbox /me /reset
  4. corr_alert_loop    — XREADs risk:correlation_alerts (transition events from
                          risk-watcher v0.9), formats + sends Telegram brief on
                          cluster_forming / cluster_resolved.
  5. inbox_pull_loop    — Gmail OAuth auto-pull (Phase 8 v0.6, dormant until creds
                          provisioned). Triages new email + archives.
  6. poly_settle_loop   — XREADs poly:settlement_alerts (auto-close events from
                          poly-adapter v0.2). Pushes booked PnL to Telegram so
                          Ben sees outcomes in real-time without checking the UI.
"""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
import sentry_sdk

from pa_agent import alerts
from pa_agent.bot import bot_loop
from pa_agent.brief import build_and_send_brief
from pa_agent.db import db
from pa_agent.inbox_loop import loop as inbox_pull_loop
from pa_agent.models import Signal
from pa_agent.settings import settings

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Mute httpx/httpcore INFO — they log full request URLs which include
    # the Telegram bot token in /bot<TOKEN>/getUpdates queries.
    # Universal default in this stack; learned the hard way 2026-05-04
    # when FRED key leaked into journald via the same path.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.0)


# ─── Loop 1: critical alerts ────────────────────────────────────────────────


async def critical_loop() -> None:
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe("signals:critical")
    log.info("Subscribed to signals:critical")

    seen: set[UUID] = set()
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        if settings.pa_agent_halt:
            continue
        try:
            raw = json.loads(message["data"])
            signal = Signal.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            log.error("Bad payload on signals:critical: %s", exc)
            continue
        if signal.id in seen:
            continue
        seen.add(signal.id)
        if len(seen) > 1000:
            seen.clear()
        try:
            await alerts.telegram(alerts.format_critical(signal))
            log.info("Critical alert sent for %s %s conf=%.2f",
                     signal.asset, signal.direction, signal.confidence)
        except Exception:
            log.exception("Failed sending critical alert for %s", signal.id)


# ─── Loop 3: correlation alerts (Phase 8 v0.2) ─────────────────────────────


async def corr_alert_loop() -> None:
    """XREAD risk:correlation_alerts from $ (latest) and surface transitions
    to Telegram. Resilient to redis hiccups via exponential backoff.
    """
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    last_id = "$"
    backoff = 1.0
    log.info("corr-alert loop starting; subscribing to %s", settings.correlation_alerts_stream)
    while True:
        try:
            result = await r.xread(
                {settings.correlation_alerts_stream: last_id},
                block=10_000,
                count=10,
            )
        except Exception:
            log.exception("corr-alert XREAD failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        if not result:
            continue
        for _stream_name, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                if settings.pa_agent_halt:
                    continue
                try:
                    raw = fields.get("data") or fields.get(b"data")
                    payload = json.loads(raw)
                    text = alerts.format_correlation_alert(payload)
                    await alerts.telegram(text)
                    log.info(
                        "corr-alert sent: transition=%s max_corr=%s",
                        payload.get("transition"), payload.get("max_corr"),
                    )
                except Exception:
                    log.exception("corr-alert process failed for %s", entry_id)


# ─── Loop 7: alpha fire alerts (Phase 8 v0.8) ─────────────────────────────


async def eod_digest_loop() -> None:
    """Once-per-day Telegram with per-strategy paper PnL across last 24h.

    Fires at brief_local_hour:brief_local_minute alongside the daily brief
    (effectively rides the same schedule). Reads `positions` table via DB
    pool; aggregates by strategy slug; formats via alerts.format_eod_digest.
    """
    if not settings.eod_digest_enabled:
        log.info("eod-digest loop: disabled via setting")
        return

    log.info(
        "eod-digest loop starting; will fire at %02d:%02d %s daily",
        settings.brief_local_hour, settings.brief_local_minute, settings.brief_timezone,
    )
    while True:
        target = _next_brief_at()
        now = datetime.now(UTC)
        delay = max(1.0, (target - now).total_seconds())
        await asyncio.sleep(delay)
        if settings.pa_agent_halt:
            log.info("eod-digest skipped — pa_agent_halt=1")
            continue
        try:
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            rows = await db.per_strategy_pnl_since(cutoff)
            row_dicts = [
                {
                    "slug": r["slug"], "status": r["status"],
                    "realized_pnl_usd": r["realized_pnl_usd"],
                    "unrealized_pnl_usd": r["unrealized_pnl_usd"],
                    "opened_at": r["opened_at"], "closed_at": r["closed_at"],
                }
                for r in rows
            ]
            text = alerts.format_eod_digest(row_dicts)
            await alerts.telegram(text)
            log.info("eod-digest sent: %d position rows", len(row_dicts))
        except Exception:
            log.exception("eod-digest failed")


# ─── Loop 9: auto-disable monitor (Phase 8 v0.9) ──────────────────────────


async def auto_disable_loop() -> None:
    """Periodically evaluates each tracked strategy's recent closed PnL;
    halts strategies whose consecutive losses or 24h drawdown exceed
    thresholds. Halt = SADD to Redis set + Telegram alert.

    The Redis set `strategy:halts` is the source of truth — alpha-fusion
    can read it to drop alphas from halted strategies (follow-up).
    For now, the alert lets the operator manually flip frontmatter to
    `inactive` if they agree with the call.
    """
    if not settings.auto_disable_enabled:
        log.info("auto-disable loop: disabled via setting")
        return
    from pa_agent import auto_disable

    strategies_to_watch = [
        s.strip() for s in settings.auto_disable_strategies.split(",") if s.strip()
    ]
    if not strategies_to_watch:
        log.info("auto-disable loop: no strategies configured; exiting")
        return

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info(
        "auto-disable loop starting; watching %d strategies, "
        "consec-loss-threshold=%d, 24h-pnl-threshold=$%.2f",
        len(strategies_to_watch),
        settings.auto_disable_consecutive_loss_threshold,
        settings.auto_disable_realized_24h_threshold,
    )

    while True:
        try:
            # Skip strategies already halted (no double-alerts)
            halted = await r.smembers(settings.auto_disable_redis_halt_set)
            halted_set = set(halted) if halted else set()
            now = auto_disable.now_utc()

            for slug in strategies_to_watch:
                if slug in halted_set:
                    continue
                if settings.pa_agent_halt:
                    continue
                try:
                    closed = await db.recent_closed_per_strategy(
                        slug, limit=settings.auto_disable_consecutive_loss_threshold,
                    )
                    pnl_24h_records = await db.recent_closed_per_strategy(slug, limit=200)
                except Exception:
                    log.exception("auto-disable.db_query_failed slug=%s", slug)
                    continue

                recent_pnls = [
                    float(r["realized_pnl_usd"]) for r in closed
                    if r.get("realized_pnl_usd") is not None
                ]
                pnls_24h = auto_disable.parse_pnls_for_24h(
                    [dict(r) for r in pnl_24h_records], now=now,
                )
                decision = auto_disable.evaluate_strategy_halt(
                    recent_closed_pnls=recent_pnls,
                    pnls_24h=pnls_24h,
                    consecutive_loss_threshold=settings.auto_disable_consecutive_loss_threshold,
                    realized_24h_threshold=settings.auto_disable_realized_24h_threshold,
                )
                if decision.should_halt:
                    try:
                        await r.sadd(settings.auto_disable_redis_halt_set, slug)
                        await alerts.telegram(
                            auto_disable.format_halt_alert(slug, decision)
                        )
                        log.warning(
                            "auto-disable: HALTED slug=%s reason=%s",
                            slug, decision.reason,
                        )
                    except Exception:
                        log.exception("auto-disable.halt_publish_failed slug=%s", slug)
        except Exception:
            log.exception("auto-disable cycle failed")
        await asyncio.sleep(settings.auto_disable_check_interval_sec)


# ─── Loop 7: alpha fire alerts (Phase 8 v0.8) ─────────────────────────────


async def alpha_alert_loop() -> None:
    """XREAD alphas:active from $ (latest); for each emission whose
    metadata.strategy_slug is in the configured allowlist, format and
    send to Telegram. Operator visibility into per-strategy fires
    without SSH-grepping the strategy-runners logs.

    Default-enabled. Disable via ALPHA_ALERTS_ENABLED=false if too noisy.
    """
    if not settings.alpha_alerts_enabled:
        log.info("alpha-alert loop: disabled via setting")
        return
    allowed = {s.strip() for s in (settings.alpha_alert_strategies or "").split(",") if s.strip()}
    if not allowed:
        log.info("alpha-alert loop: empty allowlist; exiting")
        return

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    last_id = "$"
    backoff = 1.0
    log.info(
        "alpha-alert loop starting; subscribing to %s allowlist=%s",
        settings.alphas_stream, sorted(allowed),
    )
    while True:
        try:
            result = await r.xread(
                {settings.alphas_stream: last_id},
                block=10_000,
                count=20,
            )
        except Exception:
            log.exception("alpha-alert XREAD failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        if not result:
            continue
        for _stream_name, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                if settings.pa_agent_halt:
                    continue
                try:
                    raw = fields.get("data") or fields.get(b"data")
                    alpha = json.loads(raw)
                except Exception:
                    log.warning("alpha-alert: bad payload at %s", entry_id)
                    continue
                metadata = alpha.get("metadata") or {}
                strategy_slug = metadata.get("strategy_slug") or alpha.get("strategy_slug") or ""
                if strategy_slug not in allowed:
                    continue
                try:
                    text = alerts.format_alpha_fire(alpha)
                    await alerts.telegram(text)
                    log.info(
                        "alpha-alert sent: strategy=%s direction=%s asset=%s",
                        strategy_slug,
                        alpha.get("direction"),
                        alpha.get("asset"),
                    )
                except Exception:
                    log.exception("alpha-alert process failed for %s", entry_id)


# ─── Loop 6: poly settlement alerts (Phase 8 v0.7) ─────────────────────────


async def poly_settle_loop() -> None:
    """XREAD poly:settlement_alerts from $ (latest) and forward each
    settlement event to Telegram. Same shape as corr_alert_loop —
    XREAD blocks 10s, exponential-backoff on redis hiccups, halt-aware.
    """
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    last_id = "$"
    backoff = 1.0
    log.info(
        "poly-settle loop starting; subscribing to %s",
        settings.poly_settlement_alerts_stream,
    )
    while True:
        try:
            result = await r.xread(
                {settings.poly_settlement_alerts_stream: last_id},
                block=10_000,
                count=10,
            )
        except Exception:
            log.exception("poly-settle XREAD failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 1.0
        if not result:
            continue
        for _stream_name, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                if settings.pa_agent_halt:
                    continue
                try:
                    raw = fields.get("data") or fields.get(b"data")
                    payload = json.loads(raw)
                    text = alerts.format_poly_settlement(payload)
                    await alerts.telegram(text)
                    log.info(
                        "poly-settle sent: slug=%s pnl=%s yes_won=%s",
                        payload.get("slug"),
                        payload.get("realized_pnl_usd"),
                        payload.get("yes_won"),
                    )
                except Exception:
                    log.exception("poly-settle process failed for %s", entry_id)


# ─── Loop 2: daily brief ────────────────────────────────────────────────────


def _next_brief_at() -> datetime:
    """Next datetime (UTC) at which the daily brief should fire."""
    tz = ZoneInfo(settings.brief_timezone)
    now_local = datetime.now(tz)
    target = now_local.replace(
        hour=settings.brief_local_hour,
        minute=settings.brief_local_minute,
        second=0,
        microsecond=0,
    )
    if target <= now_local:
        target += timedelta(days=1)
    return target.astimezone(UTC)


async def brief_loop() -> None:
    log.info("Brief loop started, will fire at %02d:%02d %s daily",
             settings.brief_local_hour, settings.brief_local_minute, settings.brief_timezone)
    while True:
        target = _next_brief_at()
        now = datetime.now(UTC)
        delay = max(1.0, (target - now).total_seconds())
        log.info("Next brief at %s UTC (%.0f min from now)", target.isoformat(), delay / 60)
        await asyncio.sleep(delay)
        if settings.pa_agent_halt:
            log.info("Brief skipped — pa_agent_halt=1")
            continue
        try:
            await build_and_send_brief()
        except Exception:
            log.exception("Daily brief failed")


# ─── Entry ──────────────────────────────────────────────────────────────────


async def main() -> None:
    _setup_logging()
    log.info("pa-agent starting (halt=%s)", settings.pa_agent_halt)
    await db.connect()
    try:
        await asyncio.gather(
            critical_loop(),
            brief_loop(),
            bot_loop(),
            corr_alert_loop(),
            inbox_pull_loop(),
            poly_settle_loop(),
            alpha_alert_loop(),
            eod_digest_loop(),
            auto_disable_loop(),
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

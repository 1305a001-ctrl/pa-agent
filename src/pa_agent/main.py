"""pa-agent daemon — Phase 8 v0.1.

Two concurrent loops:
  1. critical_loop  — subscribes to signals:critical, sends rich Telegram alerts
  2. brief_loop     — fires once per day at BRIEF_LOCAL_HOUR (in BRIEF_TIMEZONE),
                       summarizes the prior 24h via LLM, sends to Telegram

Future expansions: chat UI, calendar/email triage, ad-hoc Q&A.
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
from pa_agent.brief import build_and_send_brief
from pa_agent.db import db
from pa_agent.models import Signal
from pa_agent.settings import settings

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
        await asyncio.gather(critical_loop(), brief_loop())
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

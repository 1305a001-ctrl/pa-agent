"""Read-only Postgres client. pa-agent reads stack-wide tables; never writes."""
import json
import logging
from datetime import datetime

import asyncpg

from pa_agent.settings import settings

log = logging.getLogger(__name__)


class DB:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("DB not connected — call connect() first")
        return self._pool

    async def connect(self) -> None:
        if not settings.aicore_db_url:
            raise RuntimeError("AICORE_DB_URL not set")
        self._pool = await asyncpg.create_pool(
            settings.aicore_db_url, min_size=1, max_size=3, init=_init_connection,
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ─── Daily brief reads ──────────────────────────────────────────────────

    async def signals_since(self, cutoff: datetime) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT id, asset, direction, confidence, composite_risk_score,
                   redis_channel, payload, published_at
            FROM market_signals
            WHERE published_at >= $1
            ORDER BY published_at DESC
            """,
            cutoff,
        )

    async def trades_since(self, cutoff: datetime) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT id, asset, direction, status, broker, size_usd, entry_price,
                   exit_price, pnl_usd, close_reason, opened_at, closed_at
            FROM trades
            WHERE created_at >= $1
            ORDER BY created_at DESC
            """,
            cutoff,
        )

    async def poly_positions_since(self, cutoff: datetime) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT id, market_slug, side, status, stake_usd, entry_probability,
                   exit_probability, pnl_usd, resolved_outcome,
                   opened_at, closed_at
            FROM poly_positions
            WHERE created_at >= $1
            ORDER BY created_at DESC
            """,
            cutoff,
        )

    async def pipeline_runs_since(self, cutoff: datetime) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT started_at, completed_at, status, articles_fetched,
                   signals_produced, signals_published, duration_ms
            FROM pipeline_audit
            WHERE started_at >= $1
            ORDER BY started_at DESC
            """,
            cutoff,
        )


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


db = DB()

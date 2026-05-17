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

    # ─── Research check-in (Phase 8 v0.7 — R3) ──────────────────────────

    async def per_strategy_pnl_since(self, cutoff: datetime) -> list[asyncpg.Record]:
        """Per-strategy realized PnL on closed positions since cutoff.

        Used by:
          - EOD daily digest (formatted into Telegram per-strategy summary)
          - auto-disable monitor (drawdown + consecutive-loss detection)

        Joins positions → strategies; counts open + closed separately. Only
        counts realized pnl on closed positions (open unrealized swings can
        flip wildly with mark-to-mid).
        """
        return await self.pool.fetch(
            """
            SELECT
              s.slug,
              p.status,
              p.realized_pnl_usd,
              p.unrealized_pnl_usd,
              p.opened_at,
              p.closed_at
            FROM positions p
            JOIN strategies s ON s.id = p.strategy_id
            WHERE p.opened_at >= $1
            ORDER BY p.opened_at DESC
            """,
            cutoff,
        )

    async def open_positions_for_aging(self) -> list[asyncpg.Record]:
        """Open positions joined to strategies, with bucket from frontmatter.

        Used by position_aging_loop. Ordered oldest-first so the operator
        sees the most-stale positions in the alert summary.
        """
        return await self.pool.fetch(
            """
            SELECT
              p.id            AS position_id,
              p.asset,
              p.venue,
              p.opened_at,
              p.qty,
              p.mark_price,
              p.avg_entry_price,
              p.unrealized_pnl_usd,
              s.slug,
              s.frontmatter->>'bucket' AS bucket
            FROM positions p
            JOIN strategies s ON s.id = p.strategy_id
            WHERE p.status = 'open' AND p.qty > 0
            ORDER BY p.opened_at ASC
            """
        )

    async def recent_closed_per_strategy(
        self, slug: str, *, limit: int = 10,
    ) -> list[asyncpg.Record]:
        """Last N closed positions for one strategy, newest first.
        Used by auto-disable to count consecutive losses."""
        return await self.pool.fetch(
            """
            SELECT p.realized_pnl_usd, p.closed_at, p.asset
            FROM positions p
            JOIN strategies s ON s.id = p.strategy_id
            WHERE s.slug = $1 AND p.status = 'closed'
              AND p.realized_pnl_usd IS NOT NULL
            ORDER BY p.closed_at DESC
            LIMIT $2
            """,
            slug, limit,
        )

    async def signal_outcomes_since(self, cutoff: datetime) -> list[asyncpg.Record]:
        """Aggregate signal outcomes for the daily research summary.

        Joins to market_signals + strategies to surface which strategies are
        actually winning. Only counts each (signal, horizon) once via DISTINCT
        — outcome-scorer can re-score, and we don't want to double-count.
        """
        return await self.pool.fetch(
            """
            SELECT
              o.outcome,
              o.evaluation_horizon,
              o.price_change_pct,
              s.strategy_id,
              st.slug                            AS strategy_slug,
              st.frontmatter->>'bucket'          AS bucket
            FROM signal_outcomes o
            JOIN market_signals s   ON s.id = o.signal_id
            LEFT JOIN strategies st ON st.id = s.strategy_id
            WHERE o.evaluated_at >= $1
            ORDER BY o.evaluated_at DESC
            """,
            cutoff,
        )


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


db = DB()

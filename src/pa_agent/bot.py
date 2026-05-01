"""Telegram long-polling bot.

Listens for /commands DM'd to the configured bot. Only responds to the
configured chat_id — every other chat is silently ignored.

Long-polling (`getUpdates` with timeout=30s) is used so the agent doesn't
need a public webhook URL. Restart-safe: on startup we fetch with offset=-1
to skip any queued messages, so old commands never replay.

Commands:
    /help              show available commands
    /status            current open positions + last pipeline run
    /brief             fire the daily brief on demand
    /ping              health check
    /halt              stop trading-agent + poly-agent from opening new positions
    /resume            clear the halt
    /q <SQL>           read-only postgres query (PGOPTIONS-enforced)
"""
import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

import httpx
import redis.asyncio as aioredis

from pa_agent import alerts
from pa_agent.brief import build_and_send_brief
from pa_agent.db import db
from pa_agent.settings import settings

log = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 30
HALT_KEY = "system:halt"
HELP_TEXT = (
    "<b>pa-agent commands</b>\n"
    "/status   open positions + last pipeline run\n"
    "/brief    fire the daily brief now\n"
    "/halt     stop trading + poly agents from opening new positions\n"
    "/resume   clear the halt\n"
    "/q       <code>SELECT ...</code> read-only postgres query (no writes)\n"
    "/ping     health check\n"
    "/help     this message"
)

# Defence-in-depth lint for /q. Real protection is the read-only role
# enforced server-side (PGOPTIONS in db.py); this gives a friendlier error.
_WRITE_KEYWORDS_RE = re.compile(
    r"(^|[^a-z_])(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|MERGE|REINDEX|VACUUM|CLUSTER|REFRESH|LOCK|COPY)([^a-z_]|$)",
    re.IGNORECASE,
)
_STRING_LITERAL_RE = re.compile(r"'[^']*'")
_MAX_Q_ROWS = 30
_MAX_Q_CELL_LEN = 60

_redis: aioredis.Redis | None = None


def _r() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def bot_loop() -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("Telegram creds missing — bot loop disabled")
        return

    log.info("Telegram bot loop started")
    offset = await _initial_offset()

    while True:
        try:
            updates = await _get_updates(offset)
        except Exception:
            log.exception("getUpdates failed; retrying in 10s")
            await asyncio.sleep(10)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            await _handle(update)


# ─── Telegram HTTP ──────────────────────────────────────────────────────────


async def _initial_offset() -> int:
    """Skip any queued messages on startup. Returns the offset to start polling from."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates",
            params={"offset": -1, "timeout": 0},
        )
        r.raise_for_status()
        items = r.json().get("result", [])
        if not items:
            return 0
        return items[-1]["update_id"] + 1


async def _get_updates(offset: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=LONG_POLL_TIMEOUT + 5) as c:
        r = await c.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates",
            params={"offset": offset, "timeout": LONG_POLL_TIMEOUT},
        )
        r.raise_for_status()
        return r.json().get("result", [])


# ─── Command dispatch ───────────────────────────────────────────────────────


async def _handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = str((msg.get("chat") or {}).get("id"))
    if chat_id != settings.telegram_chat_id:
        log.warning("Ignoring command from unauthorized chat_id=%s", chat_id)
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    # Strip @botname suffix Telegram appends in groups: "/status@my_bot"
    cmd = text.split()[0].split("@", 1)[0].lower()
    log.info("Bot command: %s", cmd)

    if cmd == "/help" or cmd == "/start":
        await alerts.telegram(HELP_TEXT)
    elif cmd == "/ping":
        await alerts.telegram("🟢 pong")
    elif cmd == "/status":
        await alerts.telegram(await _status_text())
    elif cmd == "/brief":
        await alerts.telegram("⏳ building brief…")
        try:
            await build_and_send_brief()
        except Exception as exc:  # noqa: BLE001
            log.exception("Manual brief failed")
            await alerts.telegram(f"❌ brief failed: {exc}")
    elif cmd == "/halt":
        try:
            await _r().set(HALT_KEY, "1")
            await alerts.telegram(
                "🛑 <b>HALT</b> set\n"
                "trading-agent + poly-agent will stop opening new positions within ~5s.\n"
                "Existing positions exit normally on TP/SL/time-stop.\n"
                "Send <code>/resume</code> to clear."
            )
        except Exception as exc:  # noqa: BLE001
            await alerts.telegram(f"❌ halt failed: {exc}")
    elif cmd == "/resume":
        try:
            await _r().delete(HALT_KEY)
            await alerts.telegram(
                "▶️ <b>RESUMED</b>\n"
                "agents will pick up new signals on next message.",
            )
        except Exception as exc:  # noqa: BLE001
            await alerts.telegram(f"❌ resume failed: {exc}")
    elif cmd == "/q":
        # Strip the /q prefix and any leading whitespace
        sql = text[len("/q"):].strip()
        await alerts.telegram(await _q_text(sql))
    else:
        await alerts.telegram(f"unknown command {cmd}\n\n{HELP_TEXT}")


async def _q_text(sql: str) -> str:
    """Run a read-only postgres query and format result for Telegram."""
    if not sql:
        return ("<b>/q usage</b>\n"
                "<code>/q SELECT COUNT(*) FROM trades;</code>\n\n"
                "Read-only — INSERT/UPDATE/DELETE/etc. are refused.")

    # Lint: strip string literals first, then check for write keywords
    stripped = _STRING_LITERAL_RE.sub("", sql)
    if _WRITE_KEYWORDS_RE.search(stripped):
        return ("❌ REFUSED: query contains a write/DDL keyword.\n"
                "<code>/q</code> is read-only. Use control-plane UI for mutations.")

    # Run the query inside an explicit read-only transaction (server-side
    # enforcement — bullet-proof against CTE-smuggled writes).
    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(sql)
    except Exception as exc:  # noqa: BLE001
        return f"❌ query failed:\n<code>{_escape(str(exc)[:300])}</code>"

    if not rows:
        return "<i>(no rows)</i>"

    truncated = len(rows) > _MAX_Q_ROWS
    rows = rows[:_MAX_Q_ROWS]

    # Format as a fixed-width-ish table for Telegram (HTML <pre>)
    columns = list(rows[0].keys())
    body_rows = []
    for r in rows:
        body_rows.append([_truncate(str(r[c]) if r[c] is not None else "—") for c in columns])

    # Compute column widths
    widths = [
        max(len(c), max((len(row[i]) for row in body_rows), default=0))
        for i, c in enumerate(columns)
    ]
    header_line = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep_line = "-+-".join("-" * w for w in widths)
    lines = [header_line, sep_line]
    for row in body_rows:
        lines.append(" | ".join(row[i].ljust(widths[i]) for i in range(len(columns))))

    table = "\n".join(lines)
    note = ""
    if truncated:
        total = len(rows) + 1  # at least one more existed
        note = f"\n\n<i>{len(rows)} of {total}+ rows shown</i>"
    return f"<pre>{_escape(table)}</pre>{note}"


def _truncate(s: str) -> str:
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= _MAX_Q_CELL_LEN else s[: _MAX_Q_CELL_LEN - 1] + "…"


def _escape(s: str) -> str:
    """Escape for HTML parse_mode in Telegram."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── Status formatter ───────────────────────────────────────────────────────


async def _status_text() -> str:
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    runs = await db.pipeline_runs_since(cutoff)
    open_trades = [t for t in await db.trades_since(cutoff) if t["status"] in ("open", "pending")]
    open_polys = [
        p for p in await db.poly_positions_since(cutoff) if p["status"] in ("open", "pending")
    ]

    halted = False
    try:
        halted = bool(await _r().get(HALT_KEY))
    except Exception:  # noqa: BLE001
        pass

    lines = ["<b>📊 status</b>"]
    if halted:
        lines.append("🛑 system HALTED — agents not opening new positions")

    if runs:
        last = runs[0]
        ago_min = max(0, int((datetime.now(UTC) - last["started_at"]).total_seconds() / 60))
        lines.append(f"pipeline: last {last['status']} {ago_min}m ago "
                     f"({last['signals_produced']} signals)")
    else:
        lines.append("pipeline: <i>no runs in last 24h</i>")

    trade_exposure = sum(t["size_usd"] for t in open_trades)
    lines.append(f"trades open: {len(open_trades)} · ${trade_exposure:.0f} exposure")
    for t in open_trades[:5]:
        entry = f"@ {t['entry_price']:.2f}" if t["entry_price"] else ""
        lines.append(f"  • {t['asset']} {t['direction']} ${t['size_usd']:.0f} "
                     f"{entry} ({t['broker']})")

    poly_stake = sum(p["stake_usd"] for p in open_polys)
    lines.append(f"poly open: {len(open_polys)} · ${poly_stake:.0f} staked")
    for p in open_polys[:3]:
        prob = f"@ {p['entry_probability']:.3f}" if p["entry_probability"] else ""
        lines.append(f"  • {p['market_slug']} {p['side']} ${p['stake_usd']:.0f} {prob}")

    return "\n".join(lines)

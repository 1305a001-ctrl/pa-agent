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
"""
import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from pa_agent import alerts
from pa_agent.brief import build_and_send_brief
from pa_agent.db import db
from pa_agent.settings import settings

log = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 30
HELP_TEXT = (
    "<b>pa-agent commands</b>\n"
    "/status   open positions + last pipeline run\n"
    "/brief    fire the daily brief now\n"
    "/ping     health check\n"
    "/help     this message"
)


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
    else:
        await alerts.telegram(f"unknown command {cmd}\n\n{HELP_TEXT}")


# ─── Status formatter ───────────────────────────────────────────────────────


async def _status_text() -> str:
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    runs = await db.pipeline_runs_since(cutoff)
    open_trades = [t for t in await db.trades_since(cutoff) if t["status"] in ("open", "pending")]
    open_polys = [
        p for p in await db.poly_positions_since(cutoff) if p["status"] in ("open", "pending")
    ]

    lines = ["<b>📊 status</b>"]

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

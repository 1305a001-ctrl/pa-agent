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
    /halt              stop ALL agents from opening new positions (redis system:halt)
    /halt-strategy <slug>  pause one strategy (redis system:halt:<slug>)
    /resume            clear the global halt
    /flat              close ALL open positions to flat (publishes oms:flat-all)
    /reset-tomorrow    schedule auto-clear of system:halt at 04:00 +08
    /kill-status       show active halts + recent kill events
    /q <SQL>           read-only postgres query (PGOPTIONS-enforced)
    /run /enable /disable /timers /logs   skill runner control (cc-controller)

Kill-switch design (project_trading_stack.md, level 5 = manual):
    - All kill events publish to Redis stream `risk:alerts` for downstream
      consumers (kill_events persistence worker — Phase 1F — drains this).
    - Halts are stored as Redis keys (`system:halt`, `system:halt:<slug>`)
      so trading-agent / poly-agent can check synchronously without a DB hit.
    - /flat publishes to channel `oms:flat-all`; the OMS subscribes and
      closes all open positions at market on receipt.
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import redis.asyncio as aioredis

from pa_agent import alerts
from pa_agent.brief import build_and_send_brief
from pa_agent.db import db
from pa_agent.settings import settings

log = logging.getLogger(__name__)

LONG_POLL_TIMEOUT = 30
HALT_KEY = "system:halt"
HALT_PREFIX = "system:halt:"  # per-strategy halts: system:halt:<slug>
RISK_ALERTS_STREAM = "risk:alerts"
FLAT_CHANNEL = "oms:flat-all"
RESET_KEY = "system:halt:reset_at"  # ISO timestamp when halt should auto-clear
MYT = ZoneInfo("Asia/Kuala_Lumpur")  # +08, for /reset-tomorrow scheduling
KILL_EVENT_MAX_HISTORY = 10
HELP_TEXT = (
    "<b>pa-agent commands</b>\n"
    "<b>Kill switch (L5 — manual):</b>\n"
    "/halt              halt ALL agents (no new positions)\n"
    "/halt-strategy <code>&lt;slug&gt;</code>  pause one strategy\n"
    "/resume            clear the global halt\n"
    "/flat              close ALL open positions to flat (destructive)\n"
    "/reset-tomorrow    auto-clear /halt at 04:00 +08\n"
    "/kill-status       show active halts + recent events\n"
    "\n"
    "<b>PA (personal assistant):</b>\n"
    "/ask     <code>&lt;question&gt;</code> ask the PA — grounded in your CommandCenter\n"
    "/note    <code>&lt;text&gt;</code> capture a quick note (loaded into next /ask)\n"
    "\n"
    "<b>Ops:</b>\n"
    "/status   open positions + last pipeline run\n"
    "/brief    fire the daily brief now\n"
    "/q       <code>SELECT ...</code> read-only postgres query\n"
    "/run     <code>&lt;skill&gt;</code> fire a CommandCenter runner\n"
    "/enable  <code>&lt;skill&gt;</code> enable runner timer\n"
    "/disable <code>&lt;skill&gt;</code> disable runner timer\n"
    "/timers   show next-fire times of cc-* timers\n"
    "/logs    <code>&lt;skill&gt; [N]</code> tail last N journal lines\n"
    "/ping     health check\n"
    "/help     this message"
)

# Allowed skill names that map to cc-<name>.service on ai-primary.
# Match cc-controller/listener.sh ALLOWED_SKILLS list.
_ALLOWED_RUNNERS = {"morning-brief", "trading-research-daily", "evening-review"}

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
    elif cmd == "/ask":
        # Phase 8 v0.3 — PA Q&A grounded in CommandCenter context.
        question = text[len("/ask"):].strip()
        if not question:
            await alerts.telegram(
                "Ask me something. Examples:\n"
                "  /ask what's on my plate this week\n"
                "  /ask summarise the trading-stack pivot rationale\n"
                "  /ask draft a reply to John about the Phase 6 hand-off"
            )
        else:
            await alerts.telegram("⏳ thinking…")
            try:
                from pa_agent.pa import answer_question
                answer = await answer_question(question)
                await alerts.telegram(answer)
            except Exception as exc:  # noqa: BLE001
                log.exception("Manual /ask failed")
                await alerts.telegram(f"❌ /ask failed: {exc}")
    elif cmd == "/note":
        # Phase 8 v0.4 — append a quick note to today's inbox file.
        # Notes appear in next /ask context immediately.
        note_text = text[len("/note"):].strip()
        if not note_text:
            await alerts.telegram(
                "Capture a quick note. Example:\n"
                "  /note polymarket Hormuz market resolves in 2 days, watch for normalization\n"
                "Notes go to <code>_inbox/notes-YYYY-MM-DD.md</code> and are loaded "
                "into the next <code>/ask</code> automatically. Merge into "
                "<code>_system/memory.md</code> from your Mac when convenient."
            )
        else:
            try:
                from pa_agent.pa import append_note
                target = append_note(settings.commandcenter_path, note_text)
                if target is None:
                    await alerts.telegram(
                        "❌ /note failed (commandcenter path not writable). "
                        "Check pa-agent logs."
                    )
                else:
                    await alerts.telegram(
                        f"📝 noted to <code>_inbox/{target.name}</code>\n"
                        f"<i>(visible to next /ask immediately)</i>"
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception("Manual /note failed")
                await alerts.telegram(f"❌ /note failed: {exc}")
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
            await _emit_kill_event(kind="manual_halt_all", scope="all", reason="Telegram /halt")
            await alerts.telegram(
                "🛑 <b>HALT</b> set\n"
                "trading-agent + poly-agent will stop opening new positions within ~5s.\n"
                "Existing positions exit normally on TP/SL/trailing-stop.\n"
                "Send <code>/resume</code> to clear, <code>/flat</code> to close all."
            )
        except Exception as exc:  # noqa: BLE001
            await alerts.telegram(f"❌ halt failed: {exc}")
    elif cmd == "/halt-strategy":
        await _cmd_halt_strategy(text)
    elif cmd == "/resume":
        try:
            await _r().delete(HALT_KEY)
            await _r().delete(RESET_KEY)  # cancel any pending reset-tomorrow
            await _emit_kill_event(kind="manual_resume", scope="all", reason="Telegram /resume")
            await alerts.telegram(
                "▶️ <b>RESUMED</b>\n"
                "agents will pick up new signals on next message.",
            )
        except Exception as exc:  # noqa: BLE001
            await alerts.telegram(f"❌ resume failed: {exc}")
    elif cmd == "/flat":
        await _cmd_flat()
    elif cmd == "/reset-tomorrow":
        await _cmd_reset_tomorrow()
    elif cmd == "/kill-status":
        await alerts.telegram(await _kill_status_text())
    elif cmd == "/q":
        # Strip the /q prefix and any leading whitespace
        sql = text[len("/q"):].strip()
        await alerts.telegram(await _q_text(sql))
    elif cmd == "/run":
        await _publish_control("cc:run", text, "/run", _runner_arg_required=True)
    elif cmd == "/enable":
        await _publish_control("cc:enable", text, "/enable", _runner_arg_required=True)
    elif cmd == "/disable":
        await _publish_control("cc:disable", text, "/disable", _runner_arg_required=True)
    elif cmd == "/timers":
        await _publish_control("cc:status", text, "/timers", _runner_arg_required=False)
    elif cmd == "/logs":
        await _publish_control("cc:logs", text, "/logs", _runner_arg_required=True)
    else:
        await alerts.telegram(f"unknown command {cmd}\n\n{HELP_TEXT}")


async def _publish_control(channel: str, full_text: str, cmd_label: str,
                            *, _runner_arg_required: bool) -> None:
    """Publish a /run-style command to Redis for the host-side cc-controller.

    The cc-controller listens on these channels, validates the skill name
    against its own allowlist, runs the corresponding systemctl command,
    and Telegrams the result back DIRECTLY (so this function only confirms
    the publish; the user sees ⏳ then ✅/❌ from the controller).
    """
    parts = full_text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    if _runner_arg_required:
        if not payload:
            await alerts.telegram(f"{cmd_label} usage: <code>{cmd_label} morning-brief</code>")
            return
        # Pre-validate the first token against pa-agent's allowlist (defence in depth;
        # cc-controller has the final say). Allows /logs with optional N argument.
        skill = payload.split()[0]
        if skill not in _ALLOWED_RUNNERS:
            allowed = ", ".join(sorted(_ALLOWED_RUNNERS))
            await alerts.telegram(
                f"❌ unknown skill <code>{skill}</code>%0AAllowed: {allowed}"
            )
            return

    try:
        await _r().publish(channel, payload)
    except Exception as exc:  # noqa: BLE001
        await alerts.telegram(f"❌ {cmd_label} publish failed: {exc}")
        return

    # Light ack — the controller will Telegram the actual result.
    log.info("Published to %s: %r", channel, payload)


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


# ─── Kill switch (L5 manual) ────────────────────────────────────────────────


async def _emit_kill_event(
    *,
    kind: str,
    scope: str = "all",
    level: int = 5,
    reason: str | None = None,
    metadata: dict | None = None,
    actor: str | None = None,
) -> str:
    """Publish a KillEvent to redis stream `risk:alerts`.

    Returns the redis stream entry id. Callers should not depend on this
    write completing for user-facing acks; a separate persistence worker
    drains the stream into the kill_events postgres table.

    NOTE: pa-agent is a read-only postgres client (db.py docstring). The
    kill_events table is intentionally NOT written from here — that's the
    job of the kill-events persistence worker (Phase 1F). All audit fan-out
    flows through the redis stream first.
    """
    actor = actor or f"telegram:{settings.telegram_chat_id or 'unknown'}"
    payload = {
        "id": str(uuid.uuid4()),
        "triggered_at": datetime.now(UTC).isoformat(),
        "level": level,
        "kind": kind,
        "scope": scope,
        "actor": actor,
        "reason": reason,
        "metadata": metadata or {},
    }
    try:
        return await _r().xadd(
            RISK_ALERTS_STREAM,
            {"data": json.dumps(payload)},
            maxlen=10_000,
            approximate=True,
        )
    except Exception:  # noqa: BLE001
        log.exception("Failed to emit kill_event to %s", RISK_ALERTS_STREAM)
        return ""


_STRATEGY_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


async def _cmd_halt_strategy(full_text: str) -> None:
    """`/halt-strategy <slug>` — pause one strategy.

    The slug isn't validated against any DB list (strategies + agent_configs
    are owned by other services); we only sanity-check the format. Setting
    the halt key on an unknown slug is harmless — no consumer reads it.
    """
    parts = full_text.split(maxsplit=1)
    if len(parts) < 2:
        await alerts.telegram(
            "<b>/halt-strategy usage</b>\n"
            "<code>/halt-strategy btc-momentum</code>\n\n"
            "Sets <code>system:halt:&lt;slug&gt;</code>. The matching strategy stops opening "
            "new positions on next signal check (~5s). Existing positions exit normally."
        )
        return
    slug = parts[1].strip().lower()
    if not _STRATEGY_SLUG_RE.match(slug):
        await alerts.telegram(
            f"❌ invalid slug <code>{_escape(slug)}</code>\n"
            "Slugs are lowercase a-z, 0-9, and dashes; max 64 chars."
        )
        return
    try:
        await _r().set(f"{HALT_PREFIX}{slug}", "1")
        await _emit_kill_event(
            kind="manual_halt_strategy",
            scope=f"strategy:{slug}",
            reason=f"Telegram /halt-strategy {slug}",
            metadata={"strategy_slug": slug},
        )
        await alerts.telegram(
            f"🛑 <b>HALT-STRATEGY</b> {slug}\n"
            f"<code>system:halt:{slug}</code> set.\n"
            "Other strategies continue trading. "
            f"Clear with <code>/q DEL system:halt:{slug}</code> via redis-cli, "
            "or just <code>/resume</code> for global resume."
        )
    except Exception as exc:  # noqa: BLE001
        await alerts.telegram(f"❌ halt-strategy failed: {exc}")


async def _cmd_flat() -> None:
    """`/flat` — instruct the OMS to close ALL open positions at market.

    Publishes to channel `oms:flat-all`. The OMS subscribes to this channel
    and closes everything on receipt. ALSO sets the global halt so no new
    entries fire while we're flattening.
    """
    try:
        await _r().set(HALT_KEY, "1")
        await _r().publish(FLAT_CHANNEL, "1")
        await _emit_kill_event(
            kind="manual_flat",
            scope="all",
            reason="Telegram /flat",
            metadata={"halt_set": True, "channel": FLAT_CHANNEL},
        )
        await alerts.telegram(
            "⚠️ <b>FLAT</b> requested\n"
            "1. system:halt set (no new entries)\n"
            f"2. published to <code>{FLAT_CHANNEL}</code> — OMS will close all open at market.\n\n"
            "When the OMS hasn't yet been built, this command is a no-op for closing positions "
            "(but the halt still fires). Confirm with <code>/status</code> after."
        )
    except Exception as exc:  # noqa: BLE001
        await alerts.telegram(f"❌ flat failed: {exc}")


async def _cmd_reset_tomorrow() -> None:
    """`/reset-tomorrow` — schedule auto-clear of system:halt at 04:00 +08.

    Stores the target ISO timestamp at `system:halt:reset_at`. A separate
    scheduled worker (Phase 1F) reads this and clears HALT_KEY + RESET_KEY
    when the time arrives. Without that worker, this is informational —
    the halt does NOT auto-clear; you'd still need to /resume manually.
    """
    now = datetime.now(MYT)
    target = datetime.combine(now.date() + timedelta(days=1), time(4, 0), tzinfo=MYT)
    try:
        await _r().set(RESET_KEY, target.isoformat())
        await _emit_kill_event(
            kind="manual_reset_tomorrow",
            scope="all",
            reason=f"Auto-clear scheduled for {target.isoformat()}",
            metadata={"reset_at": target.isoformat()},
        )
        ago = target.strftime("%Y-%m-%d %H:%M %Z")
        await alerts.telegram(
            f"⏰ <b>RESET-TOMORROW</b>\n"
            f"system:halt scheduled to auto-clear at <b>{ago}</b>.\n\n"
            "<i>Requires the kill-events worker to be running (Phase 1F).</i> "
            "Until then, you'll still need to <code>/resume</code> manually. "
            "The schedule itself is logged for the dashboard."
        )
    except Exception as exc:  # noqa: BLE001
        await alerts.telegram(f"❌ reset-tomorrow failed: {exc}")


async def _kill_status_text() -> str:
    """Format the current halt state + recent kill events for Telegram."""
    redis = _r()

    # Active halts
    halt_lines: list[str] = []
    if await redis.exists(HALT_KEY):
        halt_lines.append("🛑 GLOBAL HALT (system:halt)")
    reset_at = await redis.get(RESET_KEY)
    if reset_at:
        halt_lines.append(f"⏰ auto-clear scheduled: {reset_at}")
    # Per-strategy halts — scan keys (small N, OK)
    cursor = 0
    strategy_halts: list[str] = []
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{HALT_PREFIX}*", count=100)
        for key in keys:
            if key == RESET_KEY:
                continue
            slug = key[len(HALT_PREFIX):]
            if slug:
                strategy_halts.append(slug)
        if cursor == 0:
            break
    if strategy_halts:
        halt_lines.append(
            f"🛑 strategy halts ({len(strategy_halts)}): " + ", ".join(sorted(strategy_halts))
        )
    if not halt_lines:
        halt_lines.append("✅ no active halts")

    # Recent events from risk:alerts stream (last KILL_EVENT_MAX_HISTORY)
    event_lines: list[str] = []
    try:
        entries = await redis.xrevrange(
            RISK_ALERTS_STREAM, count=KILL_EVENT_MAX_HISTORY,
        )
    except Exception:  # noqa: BLE001
        entries = []

    for entry_id, fields in entries:
        raw = fields.get("data") or fields.get(b"data")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            ev = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            continue
        ts = ev.get("triggered_at", "")[:19].replace("T", " ")
        kind = ev.get("kind", "?")
        scope = ev.get("scope", "?")
        actor = ev.get("actor", "?").split(":", 1)[-1]
        event_lines.append(f"  {ts} {kind} ({scope}) by {actor}")

    if not event_lines:
        event_lines.append("  <i>no events in stream</i>")

    return (
        "<b>🚦 Kill status</b>\n\n"
        "<b>Active halts:</b>\n" + "\n".join(halt_lines) + "\n\n"
        f"<b>Recent events (last {KILL_EVENT_MAX_HISTORY} from risk:alerts):</b>\n"
        + "\n".join(event_lines)
    )


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

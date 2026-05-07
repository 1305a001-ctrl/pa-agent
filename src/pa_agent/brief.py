"""Daily brief generator — pulls 24h of signals/trades/positions and summarizes via LLM."""
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openai import AsyncOpenAI

from pa_agent import alerts
from pa_agent.db import db
from pa_agent.settings import settings

log = logging.getLogger(__name__)


async def build_and_send_brief() -> None:
    cutoff = datetime.now(UTC) - timedelta(hours=24)

    signals = await db.signals_since(cutoff)
    trades = await db.trades_since(cutoff)
    positions = await db.poly_positions_since(cutoff)
    runs = await db.pipeline_runs_since(cutoff)

    text = _format_brief(signals, trades, positions, runs)

    # Optionally enrich with recent CommandCenter memory entries so the LLM polish
    # has Ben's evolving context (corrections, decisions, learnings).
    extra_context = ""
    if settings.commandcenter_path:
        try:
            extra_context = _load_commandcenter_memory(
                Path(settings.commandcenter_path),
                limit=settings.commandcenter_memory_entries,
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to load CommandCenter memory; continuing without")

        # Option B — calendar awareness: today's events from
        # _inbox/calendar.md surface in the morning brief if Ben maintains
        # the file. Optional; missing file = empty section.
        try:
            from pa_agent.pa import load_calendar_today
            today_cal = load_calendar_today(settings.commandcenter_path)
            if today_cal:
                extra_context += f"\n\nTODAY'S CALENDAR:\n{today_cal}"
        except Exception:  # noqa: BLE001
            log.exception("Failed to load calendar; continuing without")

    # Run LLM summary on the structured brief if creds present, else send raw
    if settings.litellm_api_key:
        try:
            text = await _llm_polish(text, extra_context=extra_context)
        except Exception:  # noqa: BLE001
            log.exception("LLM polish failed; sending raw brief")

    sent = await alerts.telegram(text)
    log.info("Daily brief sent=%s len=%d", sent, len(text))


def _load_commandcenter_memory(path: Path, limit: int = 5) -> str:
    """Return the last `limit` dated entries from CommandCenter's _system/memory.md.

    Memory entries are markdown sections starting with `## YYYY-MM-DD`. The
    file header (text before the first dated section) is dropped. Returns an
    empty string if the file is missing or has no dated entries.
    """
    memory_file = path / "_system" / "memory.md"
    if not memory_file.exists():
        return ""

    content = memory_file.read_text(encoding="utf-8")
    # Split on lines that start a dated section.
    parts = re.split(r"(?m)^(## \d{4}-\d{2}-\d{2}.*)$", content)
    # parts is [header, h1, body1, h2, body2, ...]; pair headers with bodies.
    entries: list[str] = []
    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        entries.append(f"{header}\n{body}".strip())

    if not entries:
        return ""

    return "\n\n".join(entries[-limit:])


def _format_brief(signals, trades, positions, runs) -> str:
    """Plain-text brief; the LLM optionally rewrites this into a tighter version."""
    sigs_by_dir = {"long": 0, "short": 0, "neutral": 0, "watch": 0}
    sigs_by_asset: dict[str, int] = {}
    high_conf_sigs: list = []
    for s in signals:
        sigs_by_dir[s["direction"]] = sigs_by_dir.get(s["direction"], 0) + 1
        sigs_by_asset[s["asset"]] = sigs_by_asset.get(s["asset"], 0) + 1
        if (s["confidence"] or 0) >= 0.70:
            high_conf_sigs.append(s)

    open_trades = [t for t in trades if t["status"] in ("open", "pending")]
    closed_trades = [t for t in trades if t["status"] == "closed"]
    trade_pnl = sum((t["pnl_usd"] or 0) for t in closed_trades)

    open_polys = [p for p in positions if p["status"] in ("open", "pending")]
    settled_polys = [p for p in positions if p["status"] == "closed"]
    poly_pnl = sum((p["pnl_usd"] or 0) for p in settled_polys)

    completed_runs = [r for r in runs if r["status"] == "completed"]
    failed_runs = [r for r in runs if r["status"] in ("failed", "partial")]

    out = [
        "<b>🌅 Daily brief</b> (last 24h)",
        "",
        f"<b>Pipeline</b>: {len(completed_runs)} runs ok, {len(failed_runs)} non-clean",
        f"<b>Signals</b>: {len(signals)} total",
        f"  long {sigs_by_dir['long']} · short {sigs_by_dir['short']}"
        f" · neutral {sigs_by_dir['neutral']} · watch {sigs_by_dir['watch']}",
    ]
    if sigs_by_asset:
        top = sorted(sigs_by_asset.items(), key=lambda kv: -kv[1])[:5]
        out.append("  by asset: " + ", ".join(f"{a}×{n}" for a, n in top))

    out.append("")
    out.append(f"<b>Trades</b>: {len(open_trades)} open · {len(closed_trades)} closed "
               f"· PnL ${trade_pnl:+.2f}")
    if open_trades:
        for t in open_trades[:5]:
            out.append(
                f"  • {t['asset']} {t['direction']} ${t['size_usd']:.0f} "
                f"@ {_pf(t['entry_price'])} ({t['broker']})"
            )

    out.append("")
    out.append(f"<b>Polymarket</b>: {len(open_polys)} open · {len(settled_polys)} settled"
               f" · PnL ${poly_pnl:+.2f}")
    for p in open_polys[:3]:
        out.append(f"  • {p['market_slug']} {p['side']} ${p['stake_usd']:.0f}"
                   f" @ {_pf(p['entry_probability'])}")

    if high_conf_sigs:
        out.append("")
        out.append(f"<b>High-conviction signals ≥0.70</b> ({len(high_conf_sigs)}):")
        for s in high_conf_sigs[:5]:
            out.append(
                f"  • {s['asset']} {s['direction']} conf {s['confidence']:.2f}"
                f" — {((s['payload'] or {}).get('reasoning') or '')[:80]}"
            )

    return "\n".join(out)


def _pf(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) < 1:
        return f"{x:.3f}"
    if abs(x) < 100:
        return f"{x:.2f}"
    return f"{x:,.0f}"


async def _llm_polish(structured: str, extra_context: str = "") -> str:
    """Ask the LLM to rewrite the brief into a tighter narrative under 1500 chars.

    `extra_context` is appended as recent learnings about how the user works —
    useful for personalising tone and emphasis. The LLM is instructed to use it
    as background only, not to repeat it back.
    """
    client = AsyncOpenAI(base_url=settings.litellm_base_url, api_key=settings.litellm_api_key)
    context_block = (
        f"\n\nRECENT CONTEXT (background only, do not quote back):\n{extra_context}"
        if extra_context
        else ""
    )
    prompt = (
        "You're writing a daily brief for the system owner. Below is structured data "
        "from the past 24h. Produce a 4-6 line HTML-formatted Telegram message that "
        "highlights what matters: pipeline health, open positions, any wins/losses, "
        "and one or two notable signals. If TODAY'S CALENDAR is in the context, "
        "surface the next 1-2 events with rough timing. Keep it punchy. Preserve "
        "Telegram <b> tags. No code blocks. Don't invent data not present below.\n\n"
        f"DATA:\n{structured}{context_block}"
    )
    r = await client.chat.completions.create(
        model=settings.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=400,
    )
    polished = (r.choices[0].message.content or "").strip()
    return polished or structured

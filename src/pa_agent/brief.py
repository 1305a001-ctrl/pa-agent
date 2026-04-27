"""Daily brief generator — pulls 24h of signals/trades/positions and summarizes via LLM."""
import logging
from datetime import UTC, datetime, timedelta

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

    # Run LLM summary on the structured brief if creds present, else send raw
    if settings.litellm_api_key:
        try:
            text = await _llm_polish(text)
        except Exception:  # noqa: BLE001
            log.exception("LLM polish failed; sending raw brief")

    sent = await alerts.telegram(text)
    log.info("Daily brief sent=%s len=%d", sent, len(text))


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


async def _llm_polish(structured: str) -> str:
    """Optional: ask the LLM to rewrite the brief into a tighter narrative under 1500 chars."""
    client = AsyncOpenAI(base_url=settings.litellm_base_url, api_key=settings.litellm_api_key)
    prompt = (
        "You're writing a daily brief for the system owner. Below is structured data "
        "from the past 24h. Produce a 4-6 line HTML-formatted Telegram message that "
        "highlights what matters: pipeline health, open positions, any wins/losses, "
        "and one or two notable signals. Keep it punchy. Preserve Telegram <b> tags. "
        "No code blocks. Don't invent data not present below.\n\n"
        f"DATA:\n{structured}"
    )
    r = await client.chat.completions.create(
        model=settings.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=400,
    )
    polished = (r.choices[0].message.content or "").strip()
    return polished or structured

"""Daily monitoring — pure formatter tests."""
from __future__ import annotations

from pa_agent import monitoring as mon

# ─── format_decay_halt ────────────────────────────────────────────────


def test_decay_halt_includes_slug():
    msg = mon.format_decay_halt({
        "slug": "arb-momentum-1m",
        "reason": "sharpe=-1.20_for_3cycles",
        "ttl_sec": "604800",
    })
    assert "arb-momentum-1m" in msg
    assert "sharpe=-1.20" in msg
    assert "168h" in msg     # 7 days


def test_decay_halt_handles_missing_fields():
    msg = mon.format_decay_halt({})
    assert "?" in msg   # graceful unknown slug


# ─── format_kelly_outlier ─────────────────────────────────────────────


def test_kelly_outlier_top_performer():
    msg = mon.format_kelly_outlier(
        slug="poly-sell-wings", multiplier=2.5, kind="top",
    )
    assert "TOP PERFORMER" in msg
    assert "poly-sell-wings" in msg
    assert "2.50" in msg
    assert "🚀" in msg


def test_kelly_outlier_decay():
    msg = mon.format_kelly_outlier(
        slug="losing-strategy", multiplier=0.25, kind="decay",
    )
    assert "FLOORED" in msg
    assert "0.25" in msg
    assert "🐢" in msg


# ─── format_bankroll_tier_transition ──────────────────────────────────


def test_bankroll_tier_transition_up():
    msg = mon.format_bankroll_tier_transition(
        from_tier="T1_first_profit", to_tier="T2_proven",
        pnl_usd=1500.0,
    )
    assert "T1_first_profit" in msg
    assert "T2_proven" in msg
    assert "$1,500.00" in msg
    assert "📈" in msg   # tier-up emoji


def test_bankroll_tier_transition_down():
    """Tier-down (retracement) should show the down arrow."""
    msg = mon.format_bankroll_tier_transition(
        from_tier="T2_proven", to_tier="T1_first_profit",
        pnl_usd=900.0,
    )
    assert "📉" in msg


# ─── format_oracle_latency_degradation ────────────────────────────────


def test_oracle_latency_alert_includes_p50_p95():
    msg = mon.format_oracle_latency_degradation(
        asset="btc", p50_lead_sec=0.6, p95_lead_sec=1.4,
    )
    assert "BTC" in msg
    assert "0.60" in msg
    assert "1.40" in msg
    assert "shrinking" in msg.lower()


# ─── format_daily_digest ──────────────────────────────────────────────


def test_daily_digest_includes_all_lanes():
    msg = mon.format_daily_digest(
        poly_pnl_24h=19_869.87,
        poly_closed_24h=44,
        poly_win_rate_24h=97.8,
        gmx_paper_pnl_24h=25_719_690,
        gmx_unique_whales_24h=11,
        aave_candidates_24h=25,
        bankroll_tier="T1_first_profit",
        bankroll_pnl_total=620.50,
        top_kelly=[
            ("poly-sell-wings", 2.5),
            ("poly-publisher-taker", 1.8),
        ],
        halted_strategies=4,
    )
    assert "Daily Edge Digest" in msg
    assert "Polymarket" in msg
    assert "$19,869.87" in msg
    assert "44" in msg
    assert "97.8" in msg
    assert "GMX" in msg
    assert "Aave V3" in msg
    assert "T1_first_profit" in msg
    assert "poly-sell-wings" in msg
    assert "2.50" in msg
    assert "Auto-halted strategies" in msg


def test_daily_digest_truncates_top_kelly_to_5():
    """Even with 10 entries, only top 5 should render."""
    msg = mon.format_daily_digest(
        poly_pnl_24h=0, poly_closed_24h=0, poly_win_rate_24h=0,
        gmx_paper_pnl_24h=0, gmx_unique_whales_24h=0,
        aave_candidates_24h=0,
        bankroll_tier="T0", bankroll_pnl_total=0,
        top_kelly=[(f"slug-{i}", float(i)) for i in range(10)],
        halted_strategies=0,
    )
    assert "slug-4" in msg
    assert "slug-5" not in msg   # truncated at 5


# ─── Threshold constants ──────────────────────────────────────────────


def test_thresholds_defined():
    """Constants must be reasonable defaults."""
    assert mon.KELLY_TOP_PERFORMER_THRESHOLD > 1.0
    assert mon.KELLY_DECAY_THRESHOLD < 1.0
    assert mon.KELLY_DECAY_THRESHOLD < mon.KELLY_TOP_PERFORMER_THRESHOLD
    assert mon.ORACLE_LATENCY_DEGRADATION_SEC > 0


def test_oracle_latency_asset_list():
    assert "btc" in mon.ORACLE_LATENCY_ASSETS
    assert "eth" in mon.ORACLE_LATENCY_ASSETS

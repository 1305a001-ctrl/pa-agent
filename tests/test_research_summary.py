"""Tests for _format_research_summary — the R3 research check-in block
appended to the daily brief."""
from __future__ import annotations

from pa_agent.brief import _format_brief, _format_research_summary


def _outcome(
    *,
    outcome: str = "win",
    strategy_slug: str = "btc-momentum",
    horizon: str = "24h",
    pct: float = 0.02,
) -> dict:
    return {
        "outcome": outcome,
        "evaluation_horizon": horizon,
        "price_change_pct": pct,
        "strategy_id": "00000000-0000-0000-0000-000000000000",
        "strategy_slug": strategy_slug,
        "bucket": "fast-intraday",
    }


# --- _format_research_summary ---

class TestFormatResearchSummary:
    def test_empty_outcomes_returns_no_lines(self):
        assert _format_research_summary([]) == []
        assert _format_research_summary(None) == []  # type: ignore[arg-type]

    def test_single_winner(self):
        lines = _format_research_summary([_outcome()])
        joined = "\n".join(lines)
        assert "Research" in joined
        assert "1 signals scored" in joined
        assert "win 1" in joined
        assert "loss 0" in joined
        assert "hit rate 100%" in joined

    def test_mixed_outcomes(self):
        outcomes = [
            _outcome(outcome="win") for _ in range(3)
        ] + [
            _outcome(outcome="loss") for _ in range(2)
        ] + [
            _outcome(outcome="flat") for _ in range(5)
        ] + [
            _outcome(outcome="expired") for _ in range(1)
        ]
        lines = _format_research_summary(outcomes)
        joined = "\n".join(lines)
        assert "11 signals scored" in joined
        assert "win 3" in joined
        assert "loss 2" in joined
        assert "flat 5" in joined
        assert "expired 1" in joined
        # hit rate excludes flats: 3/(3+2) = 60%
        assert "hit rate 60%" in joined

    def test_top_strategies_only_when_decisive_count_5_plus(self):
        # btc-mom: 4 wins + 1 loss = 5 decisive (qualifies)
        # eth-mom: 3 wins + 1 loss = 4 decisive (does NOT qualify)
        outcomes = (
            [_outcome(outcome="win", strategy_slug="btc-mom") for _ in range(4)]
            + [_outcome(outcome="loss", strategy_slug="btc-mom") for _ in range(1)]
            + [_outcome(outcome="win", strategy_slug="eth-mom") for _ in range(3)]
            + [_outcome(outcome="loss", strategy_slug="eth-mom") for _ in range(1)]
        )
        lines = _format_research_summary(outcomes)
        joined = "\n".join(lines)
        assert "btc-mom" in joined
        assert "eth-mom" not in joined  # below 5-decisive threshold

    def test_top_strategies_sorted_by_hit_rate(self):
        # winner: 5/5 = 100%
        # middle: 4/6 = 67%
        # loser:  2/8 = 25%
        outcomes = (
            [_outcome(outcome="win", strategy_slug="winner") for _ in range(5)]
            + [_outcome(outcome="win", strategy_slug="middle") for _ in range(4)]
            + [_outcome(outcome="loss", strategy_slug="middle") for _ in range(2)]
            + [_outcome(outcome="win", strategy_slug="loser") for _ in range(2)]
            + [_outcome(outcome="loss", strategy_slug="loser") for _ in range(6)]
        )
        lines = _format_research_summary(outcomes)
        joined = "\n".join(lines)
        # All 3 should appear (each has ≥5 decisive)
        winner_idx = joined.index("winner")
        middle_idx = joined.index("middle")
        loser_idx = joined.index("loser")
        assert winner_idx < middle_idx < loser_idx

    def test_top_strategies_capped_at_3(self):
        outcomes = []
        for i in range(10):
            outcomes.extend(
                _outcome(outcome="win", strategy_slug=f"strat-{i}")
                for _ in range(5)
            )
        lines = _format_research_summary(outcomes)
        # Count how many "strat-N" tokens appear in the strategies section.
        strat_lines = [line for line in lines if "strat-" in line]
        assert len(strat_lines) <= 3

    def test_unknown_strategy_grouped(self):
        outcomes = [
            _outcome(outcome="win", strategy_slug=None) for _ in range(3)  # type: ignore[arg-type]
        ] + [
            _outcome(outcome="loss", strategy_slug=None) for _ in range(2)  # type: ignore[arg-type]
        ]
        lines = _format_research_summary(outcomes)
        joined = "\n".join(lines)
        # 5 decisive — qualifies. <unknown> should appear.
        assert "<unknown>" in joined

    def test_all_flats_zero_hit_rate(self):
        outcomes = [_outcome(outcome="flat") for _ in range(20)]
        lines = _format_research_summary(outcomes)
        joined = "\n".join(lines)
        assert "20 signals scored" in joined
        assert "flat 20" in joined
        # Decisive=0 → hit_rate=0
        assert "hit rate 0%" in joined


# --- _format_brief integration ---

class TestFormatBriefWithResearch:
    def test_no_outcomes_omits_research_section(self):
        out = _format_brief([], [], [], [], outcomes=None)
        assert "Research" not in out

    def test_outcomes_appears_in_brief(self):
        outcomes = [_outcome(outcome="win") for _ in range(5)]
        out = _format_brief([], [], [], [], outcomes=outcomes)
        assert "Research" in out
        assert "5 signals scored" in out

    def test_legacy_call_without_outcomes_still_works(self):
        # Backward-compat: _format_brief was called without `outcomes` for
        # months. Tests + callers shouldn't break.
        out = _format_brief([], [], [], [])
        assert "Daily brief" in out
        assert "Research" not in out

"""Pure-function tests for auto_disable + EOD digest formatter."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pa_agent.alerts import format_eod_digest
from pa_agent.auto_disable import (
    DisableDecision,
    evaluate_strategy_halt,
    format_halt_alert,
    parse_pnls_for_24h,
)

# --- evaluate_strategy_halt ---


class TestEvaluateStrategyHalt:
    def test_no_halt_when_winning(self):
        d = evaluate_strategy_halt(
            recent_closed_pnls=[10.0, -2.0, 5.0, 8.0],
            pnls_24h=[10.0, -2.0, 5.0, 8.0],
            consecutive_loss_threshold=5,
            realized_24h_threshold=-100.0,
        )
        assert d.should_halt is False
        assert d.consecutive_losses == 0

    def test_halt_on_consecutive_losses(self):
        d = evaluate_strategy_halt(
            recent_closed_pnls=[-5.0, -3.0, -10.0, -2.0, -8.0, 1.0],
            pnls_24h=[-5.0, -3.0, -10.0, -2.0, -8.0, 1.0],
            consecutive_loss_threshold=5,
            realized_24h_threshold=-100.0,
        )
        assert d.should_halt is True
        assert d.consecutive_losses == 5
        assert "consecutive losses" in d.reason

    def test_halt_on_24h_drawdown(self):
        d = evaluate_strategy_halt(
            recent_closed_pnls=[-30.0, 20.0, -20.0, 5.0],
            pnls_24h=[-30.0, 20.0, -20.0, 5.0, -100.0],  # sum = -125
            consecutive_loss_threshold=5,
            realized_24h_threshold=-100.0,
        )
        assert d.should_halt is True
        assert d.realized_24h == -125.0
        assert "24h realized" in d.reason

    def test_halt_when_both_conditions_breached(self):
        d = evaluate_strategy_halt(
            recent_closed_pnls=[-50.0] * 6,
            pnls_24h=[-50.0] * 6,
            consecutive_loss_threshold=5,
            realized_24h_threshold=-100.0,
        )
        assert d.should_halt is True
        assert d.consecutive_losses == 6
        assert d.realized_24h == -300.0
        assert "consecutive" in d.reason
        assert "24h" in d.reason

    def test_consecutive_resets_on_win(self):
        # 3 losses, then a win, then 2 more losses → consecutive count = 2
        d = evaluate_strategy_halt(
            recent_closed_pnls=[-1.0, -1.0, 5.0, -1.0, -1.0, -1.0],
            pnls_24h=[],
            consecutive_loss_threshold=5,
            realized_24h_threshold=-100.0,
        )
        assert d.consecutive_losses == 2

    def test_empty_history_no_halt(self):
        d = evaluate_strategy_halt(
            recent_closed_pnls=[],
            pnls_24h=[],
            consecutive_loss_threshold=5,
            realized_24h_threshold=-100.0,
        )
        assert d.should_halt is False
        assert d.consecutive_losses == 0


# --- parse_pnls_for_24h ---


class TestParsePnlsFor24h:
    def test_filters_to_last_24h(self):
        now = datetime.now(UTC)
        records = [
            {"closed_at": now - timedelta(hours=12), "realized_pnl_usd": 5.0},  # in
            {"closed_at": now - timedelta(hours=23), "realized_pnl_usd": -3.0},  # in
            {"closed_at": now - timedelta(hours=25), "realized_pnl_usd": 100.0},  # OUT
        ]
        out = parse_pnls_for_24h(records, now=now)
        assert sorted(out) == sorted([5.0, -3.0])

    def test_skips_null_pnl(self):
        now = datetime.now(UTC)
        records = [
            {"closed_at": now - timedelta(hours=1), "realized_pnl_usd": None},
            {"closed_at": now - timedelta(hours=1), "realized_pnl_usd": 5.0},
        ]
        assert parse_pnls_for_24h(records, now=now) == [5.0]

    def test_skips_null_closed_at(self):
        now = datetime.now(UTC)
        records = [
            {"closed_at": None, "realized_pnl_usd": 5.0},
            {"closed_at": now - timedelta(hours=1), "realized_pnl_usd": 3.0},
        ]
        assert parse_pnls_for_24h(records, now=now) == [3.0]


# --- format_halt_alert ---


class TestFormatHaltAlert:
    def test_includes_key_fields(self):
        decision = DisableDecision(
            should_halt=True,
            reason="5 consecutive losses (≥ 5)",
            consecutive_losses=5,
            realized_24h=-50.0,
            n_closed_24h=10,
        )
        text = format_halt_alert("poly-fade-extreme", decision)
        assert "poly-fade-extreme" in text
        assert "5" in text
        assert "$-50" in text
        assert "Auto-halt" in text


# --- format_eod_digest ---


class TestFormatEodDigest:
    def test_empty_rows(self):
        text = format_eod_digest([])
        assert "No positions" in text

    def test_aggregates_per_strategy(self):
        rows = [
            {"slug": "poly-fade-extreme", "status": "closed",
             "realized_pnl_usd": 5.0, "unrealized_pnl_usd": None,
             "opened_at": None, "closed_at": None},
            {"slug": "poly-fade-extreme", "status": "open",
             "realized_pnl_usd": None, "unrealized_pnl_usd": -3.0,
             "opened_at": None, "closed_at": None},
            {"slug": "poly-sell-wings", "status": "closed",
             "realized_pnl_usd": 10.0, "unrealized_pnl_usd": None,
             "opened_at": None, "closed_at": None},
        ]
        text = format_eod_digest(rows)
        assert "poly-fade-extreme" in text
        assert "poly-sell-wings" in text
        assert "Total realized:" in text
        # poly-sell-wings has bigger absolute total ($10 > $5+$3=$8); appears first
        assert text.index("poly-sell-wings") < text.index("poly-fade-extreme")

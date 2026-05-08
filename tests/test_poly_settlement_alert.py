"""Tests for the poly settlement alert formatter."""
from __future__ import annotations

from pa_agent.alerts import format_poly_settlement


def _payload(**overrides) -> dict:
    base = {
        "event": "poly_position_settled",
        "position_id": "abc",
        "slug": "bitcoin-up-or-down-on-may-6-2026",
        "side": "long",
        "qty": 4028.0,
        "entry_price": 0.50,
        "final_yes_price": 1.0,
        "yes_won": True,
        "realized_pnl_usd": 2014.0,
        "reason": "closed_yes",
        "settled_at": "2026-05-06T17:16:59Z",
    }
    base.update(overrides)
    return base


class TestFormatPolySettlement:
    def test_winner_long_yes(self):
        out = format_poly_settlement(_payload())
        assert "🟢" in out
        assert "YES won" in out
        assert "bitcoin-up-or-down-on-may-6-2026" in out
        assert "+$2,014.00" in out
        assert "long" in out

    def test_loser_long_yes(self):
        out = format_poly_settlement(
            _payload(yes_won=False, final_yes_price=0.0, realized_pnl_usd=-2014.0)
        )
        assert "🔴" in out
        assert "NO won" in out
        assert "-$2,014.00" in out

    def test_winner_short_no(self):
        # Short YES at 0.84 = bought NO at 0.16. NO wins.
        out = format_poly_settlement(
            _payload(
                slug="us-iran-permanent-peace",
                side="short",
                qty=119.26,
                entry_price=0.8385,
                final_yes_price=0.0,
                yes_won=False,
                realized_pnl_usd=99.99,
                reason="past_end_no_decisive",
            )
        )
        assert "🟢" in out
        assert "NO won" in out
        assert "short" in out
        assert "+$99.99" in out

    def test_break_even_neutral_icon(self):
        out = format_poly_settlement(_payload(realized_pnl_usd=0.0))
        # When PnL is exactly 0, we use the neutral white icon.
        assert "⚪" in out
        # And neither green nor red.
        assert "🟢" not in out
        assert "🔴" not in out

    def test_unknown_yes_won_treated_as_question(self):
        out = format_poly_settlement(_payload(yes_won=None))
        assert "?" in out

    def test_reason_shown_in_italics(self):
        out = format_poly_settlement(
            _payload(reason="past_end_yes_decisive")
        )
        assert "via past_end_yes_decisive" in out

    def test_qty_formatted_with_two_decimals(self):
        out = format_poly_settlement(_payload(qty=119.2606))
        assert "qty <b>119.26</b>" in out

    def test_entry_and_final_shown_with_4_decimals(self):
        out = format_poly_settlement(
            _payload(entry_price=0.8385, final_yes_price=0.6755)
        )
        assert "0.8385" in out
        assert "0.6755" in out

    def test_html_telegram_compatible(self):
        # Output should use <b>, <code>, <i> tags only — no other HTML.
        out = format_poly_settlement(_payload())
        # No raw <, > in text other than our tags.
        # (Crude check — Telegram HTML parser is lenient but better safe.)
        assert "</b>" in out
        assert "<code>" in out

    def test_missing_fields_default_safely(self):
        # Defensive: malformed payload doesn't crash.
        out = format_poly_settlement({})
        assert "Poly settled" in out

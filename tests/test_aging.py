"""Tests for the position-aging pure helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pa_agent import aging


def _row(
    *,
    pid: str = "p1",
    slug: str = "btc-momentum",
    bucket: str | None = "fast-intraday",
    venue: str = "binance",
    asset: str = "BTC-USDT",
    age_hours: float = 7.0,
    upnl: float | None = -1.5,
    now: datetime,
) -> dict:
    return {
        "position_id": pid,
        "slug": slug,
        "bucket": bucket,
        "venue": venue,
        "asset": asset,
        "opened_at": now - timedelta(hours=age_hours),
        "qty": 0.01,
        "mark_price": 80000.0,
        "avg_entry_price": 79500.0,
        "unrealized_pnl_usd": upnl,
    }


NOW = datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC)


def test_fast_intraday_position_at_6h_alerts_at_first_tier():
    rows = [_row(age_hours=7.0, now=NOW)]
    out = aging.evaluate_aging(rows, now=NOW, already_alerted=set())
    assert len(out) == 1
    assert out[0].tier_hours == 6.0
    assert out[0].alert_key == "p1:6"


def test_position_under_first_tier_yields_no_alert():
    rows = [_row(age_hours=2.0, now=NOW)]
    assert aging.evaluate_aging(rows, now=NOW, already_alerted=set()) == []


def test_polymarket_positions_are_excluded():
    rows = [_row(venue="polymarket", asset="bitcoin-up-or-down", age_hours=240, now=NOW)]
    assert aging.evaluate_aging(rows, now=NOW, already_alerted=set()) == []


def test_no_re_alert_at_same_tier():
    rows = [_row(age_hours=12.0, now=NOW)]  # past 6h tier
    out1 = aging.evaluate_aging(rows, now=NOW, already_alerted={"p1:6"})
    assert out1 == []  # already alerted at 6h


def test_promotes_to_next_tier_when_first_tier_already_alerted():
    rows = [_row(age_hours=30.0, now=NOW)]  # past 6h AND 24h tiers
    out = aging.evaluate_aging(rows, now=NOW, already_alerted={"p1:6"})
    assert len(out) == 1
    assert out[0].tier_hours == 24.0


def test_unknown_bucket_uses_default_tiers():
    rows = [_row(bucket=None, age_hours=25.0, now=NOW)]
    out = aging.evaluate_aging(rows, now=NOW, already_alerted=set())
    assert len(out) == 1
    assert out[0].tier_hours == 24.0  # default first tier


def test_swing_bucket_does_not_alert_at_6h():
    rows = [_row(bucket="swing", age_hours=12.0, now=NOW)]
    assert aging.evaluate_aging(rows, now=NOW, already_alerted=set()) == []


def test_multiple_positions_alert_independently():
    rows = [
        _row(pid="p1", age_hours=7.0, now=NOW),
        _row(pid="p2", age_hours=2.0, now=NOW),
        _row(pid="p3", age_hours=30.0, now=NOW),
    ]
    out = aging.evaluate_aging(rows, now=NOW, already_alerted=set())
    assert {a.position_id for a in out} == {"p1", "p3"}


def test_format_aging_alert_renders_html():
    rows = [_row(age_hours=8.0, now=NOW)]
    alerts = aging.evaluate_aging(rows, now=NOW, already_alerted=set())
    body = aging.format_aging_alert(alerts)
    assert "Stale open positions" in body
    assert "btc-momentum" in body
    assert "BTC-USDT" in body
    assert "fast-intraday" in body


def test_format_aging_alert_empty_yields_empty_string():
    assert aging.format_aging_alert([]) == ""

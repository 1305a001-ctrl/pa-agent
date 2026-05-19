"""Pure-logic tests for the trading:events forwarder formatter.

The corresponding loop (trading_events_loop in main.py) follows the same
shape as corr_alert_loop / poly_settle_loop — XREAD + Telegram send —
which the other test files already exercise indirectly. These tests cover
the new formatter and its edge cases (html-escape, missing fields).
"""

from pa_agent.alerts import format_trading_event


def test_format_drawdown_alert_includes_icon_and_kind():
    text = format_trading_event({
        "source": "drawdown_monitor",
        "kind": "drawdown_alert",
        "msg": "NAV dropped 6.2% from high-water $612.45 (now $574.50)",
        "ts": "1747700000",
    })
    assert "📉" in text
    assert "drawdown_alert" in text
    assert "drawdown_monitor" in text
    assert "NAV dropped 6.2%" in text


def test_format_daily_summary_includes_icon():
    text = format_trading_event({
        "source": "daily_pnl_digest",
        "kind": "daily_summary",
        "msg": "Last 24h: +$42.50 (7 closed, 71% wins). PUSD: $510.20",
    })
    assert "📊" in text
    assert "daily_summary" in text
    assert "+$42.50" in text


def test_format_unknown_kind_uses_default_icon():
    text = format_trading_event({
        "source": "watchdog",
        "kind": "novel_event",
        "msg": "something new happened",
    })
    assert "•" in text
    assert "novel_event" in text


def test_format_missing_fields_does_not_crash():
    text = format_trading_event({})
    assert "?" in text
    assert "(no msg)" in text


def test_format_html_escapes_msg_special_chars():
    """Stray `<`, `>`, `&` would break parse_mode=HTML — escape them."""
    text = format_trading_event({
        "source": "watchdog",
        "kind": "container_unhealthy",
        "msg": "poly-adapter: status <starting> & restarting",
    })
    assert "&lt;starting&gt;" in text
    assert "&amp;" in text


def test_format_preserves_kind_in_known_icons():
    """Known kinds get specific icons; verify two representative."""
    crash = format_trading_event({"source": "x", "kind": "container_crashed", "msg": "x"})
    assert "💥" in crash
    halt = format_trading_event({"source": "x", "kind": "emergency_halt", "msg": "x"})
    assert "🛑" in halt

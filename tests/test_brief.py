from datetime import UTC, datetime, timedelta
from pathlib import Path

from pa_agent.brief import _format_brief, _load_commandcenter_memory, _pf
from pa_agent.main import _next_brief_at
from pa_agent.settings import settings


def _row(**fields) -> dict:
    return fields


def test_format_brief_empty():
    out = _format_brief([], [], [], [])
    assert "Daily brief" in out
    assert "0 runs ok" in out
    assert "0 total" in out
    assert "0 open" in out


def test_format_brief_with_data():
    sigs = [
        _row(asset="BTC", direction="long", confidence=0.8,
             composite_risk_score=0.7, redis_channel="signals:trading",
             payload={"reasoning": "rally", "strategy_name": "btc-mom"},
             published_at=datetime.now(UTC)),
        _row(asset="NVDA", direction="long", confidence=0.62,
             composite_risk_score=0.6, redis_channel="signals:trading",
             payload={}, published_at=datetime.now(UTC)),
    ]
    trades = [
        _row(asset="BTC", direction="long", status="open", broker="paper",
             size_usd=50, entry_price=78000, exit_price=None, pnl_usd=None,
             close_reason=None, opened_at=datetime.now(UTC), closed_at=None),
    ]
    polys = []
    runs = [_row(started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
                 status="completed", articles_fetched=399, signals_produced=2,
                 signals_published=2, duration_ms=12345)]

    out = _format_brief(sigs, trades, polys, runs)
    assert "BTC×1" in out and "NVDA×1" in out
    assert "1 runs ok" in out
    assert "1 open" in out
    assert "$50" in out
    assert "high-conviction" in out.lower()  # the 0.80 signal lands in high-conf section


def test_format_brief_pnl_sign():
    closed = [_row(asset="BTC", direction="long", status="closed", broker="paper",
                   size_usd=50, entry_price=78000, exit_price=80000, pnl_usd=2.5,
                   close_reason="tp", opened_at=datetime.now(UTC),
                   closed_at=datetime.now(UTC))]
    out = _format_brief([], closed, [], [])
    assert "PnL $+2.50" in out


def test_pf_formatting():
    assert _pf(None) == "—"
    assert _pf(0.525) == "0.525"
    assert _pf(78014.95) == "78,015"
    assert _pf(1.5) == "1.50"


def test_load_commandcenter_memory_missing_path(tmp_path: Path):
    # Path exists but no _system/memory.md inside
    out = _load_commandcenter_memory(tmp_path)
    assert out == ""


def test_load_commandcenter_memory_no_dated_entries(tmp_path: Path):
    (tmp_path / "_system").mkdir()
    (tmp_path / "_system" / "memory.md").write_text("# Memory\n\nNo dated sections here.\n")
    assert _load_commandcenter_memory(tmp_path) == ""


def test_load_commandcenter_memory_returns_last_n(tmp_path: Path):
    (tmp_path / "_system").mkdir()
    (tmp_path / "_system" / "memory.md").write_text(
        "# Memory — header text\n\n"
        "## 2026-04-28 — first\nbody one\n\n"
        "## 2026-04-29 — second\nbody two\n\n"
        "## 2026-04-30 — third\nbody three\n"
    )
    out = _load_commandcenter_memory(tmp_path, limit=2)
    # Header dropped; only last two entries returned in order.
    assert "## 2026-04-28 — first" not in out
    assert "## 2026-04-29 — second" in out
    assert "## 2026-04-30 — third" in out
    assert "header text" not in out


def test_load_commandcenter_memory_limit_larger_than_entries(tmp_path: Path):
    (tmp_path / "_system").mkdir()
    (tmp_path / "_system" / "memory.md").write_text("## 2026-04-30 — only entry\nbody\n")
    out = _load_commandcenter_memory(tmp_path, limit=5)
    assert "## 2026-04-30 — only entry" in out
    assert "body" in out


def test_next_brief_returns_future_utc():
    t = _next_brief_at()
    now = datetime.now(UTC)
    assert t > now
    assert t < now + timedelta(days=2)
    assert t.tzinfo is not None
    # local hour should match settings
    from zoneinfo import ZoneInfo
    local = t.astimezone(ZoneInfo(settings.brief_timezone))
    assert local.hour == settings.brief_local_hour
    assert local.minute == settings.brief_local_minute

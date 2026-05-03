"""Unit tests for the L5 manual kill-switch commands.

Focused on pure-logic pieces that don't need redis/alerts mocking:
- _STRATEGY_SLUG_RE shape validation
- HELP_TEXT mentions the new commands
- Constants are well-formed (no typos)
- _emit_kill_event payload structure (via inspecting what xadd would send)

End-to-end command behaviour (redis+alerts.telegram) is covered by the
integration tests + manual smoke once deployed.
"""
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from pa_agent import bot


def test_strategy_slug_regex_accepts_typical_slugs():
    """Valid: lowercase a-z, 0-9, dashes; 1-64 chars; can't start with dash."""
    valid = [
        "btc-momentum",
        "eth-momentum",
        "crypto-mean-reversion",
        "poly-btc-price-target",
        "nvda-trend-v2",
        "a",
        "x" * 64,
        "abc123",
    ]
    for slug in valid:
        assert bot._STRATEGY_SLUG_RE.match(slug), f"should accept {slug!r}"


def test_strategy_slug_regex_rejects_garbage():
    """Reject: empty, uppercase, spaces, special chars, leading dash, too long."""
    invalid = [
        "",
        "BTC-MOMENTUM",
        "btc momentum",
        "btc;DROP",
        "-leading-dash",
        "x" * 65,
        "../etc/passwd",
        "btc/momentum",
    ]
    for slug in invalid:
        assert not bot._STRATEGY_SLUG_RE.match(slug), f"should reject {slug!r}"


def test_help_text_mentions_all_kill_commands():
    text = bot.HELP_TEXT
    assert "/halt" in text
    assert "/halt-strategy" in text
    assert "/resume" in text
    assert "/flat" in text
    assert "/reset-tomorrow" in text
    assert "/kill-status" in text


def test_kill_switch_constants_are_well_formed():
    assert bot.HALT_KEY == "system:halt"
    assert bot.HALT_PREFIX == "system:halt:"
    assert bot.HALT_PREFIX.startswith(bot.HALT_KEY)
    assert bot.RISK_ALERTS_STREAM == "risk:alerts"
    assert bot.FLAT_CHANNEL == "oms:flat-all"
    assert bot.RESET_KEY.startswith(bot.HALT_PREFIX)


def test_myt_zone_is_kuala_lumpur():
    """Reset-tomorrow uses MYT (+08) — confirm the zone is what we think."""
    now = datetime(2026, 5, 3, 12, 0, tzinfo=bot.MYT)
    assert now.utcoffset().total_seconds() == 8 * 3600
    assert "Kuala_Lumpur" in str(bot.MYT)


@pytest.mark.asyncio
async def test_emit_kill_event_builds_canonical_payload():
    """Patch the redis xadd call; assert payload has all required fields."""
    captured: dict = {}

    fake_redis = AsyncMock()

    async def _xadd(stream, fields, **_kwargs):
        captured["stream"] = stream
        captured["fields"] = fields
        return "1-0"

    fake_redis.xadd = _xadd

    with patch.object(bot, "_r", return_value=fake_redis):
        with patch.object(bot.settings, "telegram_chat_id", "12345"):
            await bot._emit_kill_event(
                kind="manual_halt_all",
                scope="all",
                reason="Telegram /halt",
                metadata={"source": "test"},
            )

    assert captured["stream"] == "risk:alerts"
    raw = captured["fields"]["data"]
    payload = json.loads(raw)

    assert payload["kind"] == "manual_halt_all"
    assert payload["scope"] == "all"
    assert payload["level"] == 5  # default for manual
    assert payload["actor"] == "telegram:12345"
    assert payload["reason"] == "Telegram /halt"
    assert payload["metadata"] == {"source": "test"}
    assert "id" in payload
    assert "triggered_at" in payload
    # triggered_at must be ISO-parseable
    assert datetime.fromisoformat(payload["triggered_at"]).tzinfo is not None

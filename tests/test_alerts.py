from datetime import UTC, datetime
from uuid import uuid4

from pa_agent.alerts import format_critical
from pa_agent.models import Signal


def _signal(**ov) -> Signal:
    base = dict(
        id=uuid4(), strategy_id=uuid4(), research_config_id=uuid4(),
        strategy_git_sha="abc", research_config_version=1,
        asset="BTC", direction="long", confidence=0.92,
        composite_risk_score=0.88,
        risk_score={"source_credibility": 0.9, "narrative_novelty": 0.8,
                    "timing_precision": 0.9, "evidence_strength": 0.9},
        source_article_ids=[uuid4(), uuid4(), uuid4()],
        payload={"reasoning": "Three credible sources confirm a coordinated rally narrative.",
                 "strategy_name": "BTC Breakout"},
        published_at=datetime.now(UTC),
    )
    base.update(ov)
    return Signal.model_validate(base)


def test_critical_message_contains_key_fields():
    msg = format_critical(_signal())
    assert "CRITICAL" in msg
    assert "BTC" in msg
    assert "LONG" in msg
    assert "0.92" in msg
    assert "0.88" in msg
    assert "BTC Breakout" in msg
    assert "credible sources" in msg
    assert "3 article" in msg


def test_critical_short_signal():
    msg = format_critical(_signal(direction="short", confidence=0.71, composite_risk_score=0.86))
    assert "SHORT" in msg
    assert "📉" in msg


def test_critical_no_reasoning_no_articles():
    msg = format_critical(_signal(payload={"strategy_name": "x"}, source_article_ids=[]))
    assert "BTC" in msg
    assert "Based on" not in msg


def test_signal_validator_parses_string_risk_score():
    """Strings should be json.loads'd into dicts (defense for ad-hoc republish)."""
    s = Signal.model_validate({
        "id": str(uuid4()), "strategy_id": str(uuid4()), "research_config_id": str(uuid4()),
        "strategy_git_sha": "abc", "research_config_version": 1,
        "asset": "BTC", "direction": "long", "confidence": 0.7,
        "composite_risk_score": 0.7,
        "risk_score": '{"source_credibility": 0.5}',
        "payload": '{"reasoning": "x"}',
        "source_article_ids": [], "published_at": datetime.now(UTC).isoformat(),
    })
    assert isinstance(s.risk_score, dict)
    assert s.risk_score == {"source_credibility": 0.5}
    assert s.payload == {"reasoning": "x"}

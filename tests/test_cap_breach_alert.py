"""Tests for the cap-breach alert formatter."""
from __future__ import annotations

from pa_agent.alerts import format_cap_breach


def _payload_cluster() -> dict:
    return {
        "ts": "2026-05-09T06:33:00+00:00",
        "reason": "cluster_exposure_cap_exceeded",
        "strategy_slug": "poly-sell-wings",
        "asset": "bitcoin-above-78k-on-may-9",
        "venue": "polymarket",
        "bucket": "poly-bet",
        "cluster": "poly:bitcoin",
        "snapshot": {
            "cluster": "poly:bitcoin",
            "cap_usd": 800.0,
            "current_exposure_usd": 700.0,
            "would_be_exposure_usd": 900.0,
            "proposed_notional_usd": 200.0,
        },
        "alpha_id": "abc",
    }


def _payload_bucket() -> dict:
    p = _payload_cluster()
    p["reason"] = "bucket_exposure_cap_exceeded"
    p["snapshot"] = {
        "bucket": "poly-bet",
        "cap_usd": 2000.0,
        "current_exposure_usd": 1900.0,
        "would_be_exposure_usd": 2100.0,
        "proposed_notional_usd": 200.0,
    }
    return p


def test_cluster_breach_renders_cluster_label():
    body = format_cap_breach(_payload_cluster())
    assert "Cluster cap" in body
    assert "poly:bitcoin" in body
    assert "$800" in body
    assert "$900" in body
    assert "$200" in body


def test_bucket_breach_renders_bucket_label():
    body = format_cap_breach(_payload_bucket())
    assert "Bucket cap" in body
    assert "poly-bet" in body
    assert "$2,000" in body or "$2000" in body
    assert "$2,100" in body or "$2100" in body


def test_breach_includes_strategy_and_asset():
    body = format_cap_breach(_payload_cluster())
    assert "poly-sell-wings" in body
    assert "bitcoin-above-78k-on-may-9" in body
    assert "polymarket" in body


def test_breach_handles_missing_snapshot_safely():
    payload = {
        "reason": "cluster_exposure_cap_exceeded",
        "strategy_slug": "x",
        "asset": "y",
        "venue": "polymarket",
        "cluster": "poly:eth",
    }
    body = format_cap_breach(payload)
    # When snapshot is missing, we still produce a sensible message.
    assert "poly:eth" in body
    assert "$0" in body  # all numbers default to 0

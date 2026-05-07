"""Pure-function tests for format_correlation_alert."""
from pa_agent.alerts import format_correlation_alert


def _payload(**overrides) -> dict:
    base = {
        "ts": "2026-05-07T12:00:00Z",
        "transition": "cluster_forming",
        "max_corr": 0.9243,
        "cluster_count": 1,
        "threshold": 0.85,
        "universe_size": 19,
        "top_pairs": [
            {"a": "BTC-USDT", "b": "ETH-USDT", "rho": 0.95},
            {"a": "NVDA", "b": "SPY", "rho": 0.88},
        ],
    }
    base.update(overrides)
    return base


def test_cluster_forming_uses_warning_icon():
    out = format_correlation_alert(_payload(transition="cluster_forming"))
    assert "⚠️" in out
    assert "RISK CLUSTER FORMING" in out


def test_cluster_resolved_uses_check_icon():
    out = format_correlation_alert(_payload(transition="cluster_resolved"))
    assert "✅" in out
    assert "Cluster resolved" in out


def test_max_corr_rendered_three_decimals():
    out = format_correlation_alert(_payload(max_corr=0.9243))
    assert "0.924" in out


def test_threshold_rendered_two_decimals():
    out = format_correlation_alert(_payload(threshold=0.85))
    assert "0.85" in out


def test_max_corr_none_skips_value():
    """Defensive: empty universe → max_corr=None. Render w/o crashing."""
    out = format_correlation_alert(_payload(max_corr=None))
    assert "threshold" in out


def test_top_pairs_truncated_to_5():
    pairs = [{"a": f"X{i}", "b": f"Y{i}", "rho": 0.9 - i * 0.01} for i in range(10)]
    out = format_correlation_alert(_payload(top_pairs=pairs))
    # Each pair line starts with "  •" — count them
    assert out.count("  • ") == 5


def test_no_top_pairs_omits_section():
    out = format_correlation_alert(_payload(top_pairs=[]))
    assert "Top pairs by |ρ|" not in out


def test_negative_rho_renders_with_sign():
    pairs = [{"a": "A", "b": "B", "rho": -0.91}]
    out = format_correlation_alert(_payload(top_pairs=pairs))
    assert "-0.910" in out

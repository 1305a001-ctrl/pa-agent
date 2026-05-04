"""Tests for bot.py command handlers — /q lint + /run-style publish gating."""
from __future__ import annotations

import re

import pytest

from pa_agent import bot
from pa_agent.bot import (
    _ALLOWED_RUNNERS,
    _STRING_LITERAL_RE,
    _WRITE_KEYWORDS_RE,
    _q_text,
)

# ─── /q lint regex ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql,should_block",
    [
        # Basic writes
        ("INSERT INTO trades VALUES (1)", True),
        ("UPDATE trades SET pnl_usd = 999", True),
        ("DELETE FROM trades", True),
        ("TRUNCATE TABLE trades", True),
        ("DROP TABLE trades", True),
        ("ALTER TABLE trades ADD COLUMN x INT", True),
        ("CREATE TABLE foo (id INT)", True),
        ("GRANT SELECT ON trades TO public", True),
        ("REVOKE SELECT ON trades FROM public", True),
        ("MERGE INTO trades USING ...", True),
        ("REINDEX TABLE trades", True),
        ("VACUUM trades", True),
        ("CLUSTER trades", True),
        ("REFRESH MATERIALIZED VIEW foo", True),
        ("LOCK TABLE trades", True),
        ("COPY trades FROM stdin", True),
        # Smuggling attempts (the original attack vector that wiped trades)
        ("WITH x AS (DELETE FROM trades RETURNING *) SELECT * FROM x", True),
        ("SELECT 1; DELETE FROM trades", True),
        ("/* hi */ DELETE FROM trades", True),
        # Reads should pass
        ("SELECT COUNT(*) FROM trades", False),
        ("SELECT * FROM market_signals ORDER BY published_at DESC LIMIT 5", False),
        ("SELECT a.id, b.name FROM trades a JOIN strategies b ON a.signal_id = b.id", False),
        # String-literal false-positive guard
        ("SELECT 'this is not a real DELETE' AS msg", False),
        ("SELECT 'INSERT INTO x' AS demo", False),
        ("SELECT 'TRUNCATE' AS keyword_ref", False),
        # Identifier with embedded write-keyword should not match
        ("SELECT delete_after FROM table_x", False),
        ("SELECT _INSERT_PLACEHOLDER FROM table_x", False),
    ],
)
def test_q_lint_regex(sql: str, should_block: bool):
    """The lint regex strips string literals first, then matches write keywords."""
    stripped = _STRING_LITERAL_RE.sub("", sql)
    matched = bool(_WRITE_KEYWORDS_RE.search(stripped))
    assert matched == should_block, f"sql={sql!r} should_block={should_block} matched={matched}"


# ─── _q_text behaviour ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_q_text_empty_arg_shows_usage():
    out = await _q_text("")
    assert "/q usage" in out
    # The usage block does mention INSERT in passing ("read-only — INSERT/UPDATE/...
    # are refused"). The intent of this check is "the empty case isn't running a
    # query" — confirm by checking it's the usage block, not a result table.
    assert "<pre>" not in out


@pytest.mark.asyncio
async def test_q_text_blocked_query():
    out = await _q_text("DELETE FROM trades")
    assert "REFUSED" in out
    assert "read-only" in out


@pytest.mark.asyncio
async def test_q_text_cte_smuggled_delete():
    """The exact attack that wiped trades on 2026-05-01."""
    out = await _q_text("WITH x AS (DELETE FROM trades RETURNING *) SELECT * FROM x")
    assert "REFUSED" in out


@pytest.mark.asyncio
async def test_q_text_string_literal_passes_lint(monkeypatch):
    """A SELECT containing 'DELETE' inside a string literal should pass the lint
    and reach the DB layer (which would then run it). We mock the DB layer so
    the test doesn't need a real connection.
    """
    class _FakeRow(dict):
        def keys(self):
            return ["msg"]

        def __getitem__(self, key):
            return "this is not a real DELETE"

    class _FakeConn:
        async def fetch(self, sql):
            return [_FakeRow()]

        def transaction(self, **kwargs):
            class _Ctx:
                async def __aenter__(self_):
                    return self_

                async def __aexit__(self_, *a):
                    return None

            assert kwargs.get("readonly") is True, "must use readonly transaction"
            return _Ctx()

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_):
                    return _FakeConn()

                async def __aexit__(self_, *a):
                    return None

            return _Ctx()

    # bot.db is a module-level singleton; pool is a @property that raises if
    # _pool is None. Set the underlying attribute directly.
    monkeypatch.setattr(bot.db, "_pool", _FakePool())

    out = await _q_text("SELECT 'this is not a real DELETE' AS msg")
    # Output is HTML <pre> table. Should contain the result, not REFUSED.
    assert "REFUSED" not in out
    assert "this is not a real DELETE" in out


# ─── _ALLOWED_RUNNERS allowlist ─────────────────────────────────────────────


def test_allowed_runners_set():
    """Allowlist must match cc-controller/listener.sh ALLOWED_SKILLS."""
    expected = {"morning-brief", "trading-research-daily", "evening-review"}
    assert _ALLOWED_RUNNERS == expected


# ─── escape helper for HTML output ──────────────────────────────────────────


def test_escape_renders_html_safely():
    from pa_agent.bot import _escape

    assert _escape("<script>") == "&lt;script&gt;"
    assert _escape("a & b") == "a &amp; b"
    assert _escape("plain text") == "plain text"


def test_truncate_caps_long_cells():
    from pa_agent.bot import _MAX_Q_CELL_LEN, _truncate

    short = "abc"
    assert _truncate(short) == short

    long_str = "x" * (_MAX_Q_CELL_LEN + 50)
    out = _truncate(long_str)
    assert len(out) == _MAX_Q_CELL_LEN
    assert out.endswith("…")


def test_truncate_collapses_newlines():
    """Cells with embedded newlines should display as single-line."""
    from pa_agent.bot import _truncate

    s = "line one\nline two\rline three"
    out = _truncate(s)
    assert "\n" not in out
    assert "\r" not in out
    assert out == "line one line two line three"


# ─── Help text contents ─────────────────────────────────────────────────────


def test_help_text_lists_v0_3_commands():
    """Sanity check that the help message references the new v0.3 commands."""
    h = bot.HELP_TEXT
    for cmd in ("/q", "/run", "/enable", "/disable", "/timers", "/logs"):
        assert cmd in h, f"help text missing {cmd}"


def test_write_keywords_regex_is_case_insensitive():
    """The regex must match lowercase write keywords too."""
    assert _WRITE_KEYWORDS_RE.flags & re.IGNORECASE
    assert _WRITE_KEYWORDS_RE.search(" delete from x ")
    assert _WRITE_KEYWORDS_RE.search(" Delete From X ")

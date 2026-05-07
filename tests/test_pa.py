"""Pure-function tests for pa.load_pa_context + build_ask_prompt."""
from pathlib import Path

import pytest

from pa_agent.pa import (
    _read_file_capped,
    build_ask_prompt,
    load_pa_context,
)


@pytest.fixture
def fake_cc(tmp_path: Path) -> Path:
    """A minimal CommandCenter directory layout for tests."""
    (tmp_path / "_context").mkdir()
    (tmp_path / "_system").mkdir()
    (tmp_path / "_context" / "about-me.md").write_text(
        "# About Ben\n\n- Founder of 7 ventures\n- Based in Kuala Lumpur\n",
        encoding="utf-8",
    )
    (tmp_path / "_context" / "about-businesses.md").write_text(
        "# Ventures\n\n## the2357\nTrading platform.\n",
        encoding="utf-8",
    )
    (tmp_path / "_context" / "voice.md").write_text(
        "# Voice\n\nCasual, concise. No marketing-speak.\n",
        encoding="utf-8",
    )
    (tmp_path / "_system" / "memory.md").write_text(
        "# Memory\n\n## 2026-05-06: Trading-stack pivot\nBuilt Phase A1.\n\n"
        "## 2026-05-07: PA build kicked off\nFirst /ask command.\n",
        encoding="utf-8",
    )
    return tmp_path


# --- _read_file_capped -----------------------------------------------------


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert _read_file_capped(tmp_path / "nope.md", 100) == ""


def test_read_full_file_under_cap(tmp_path: Path):
    f = tmp_path / "x.md"
    f.write_text("hello world", encoding="utf-8")
    assert _read_file_capped(f, 100) == "hello world"


def test_read_caps_long_file_at_paragraph_boundary(tmp_path: Path):
    f = tmp_path / "x.md"
    f.write_text("a" * 50 + "\n\n" + "b" * 200, encoding="utf-8")
    out = _read_file_capped(f, 100)
    # Should cut at the \n\n boundary (50) rather than mid-bbb.
    assert out.startswith("a" * 50)
    assert "…(truncated)" in out
    assert "bb" not in out  # everything after \n\n was dropped


def test_read_caps_at_hard_limit_when_no_paragraph(tmp_path: Path):
    f = tmp_path / "x.md"
    f.write_text("x" * 1000, encoding="utf-8")
    out = _read_file_capped(f, 100)
    # No \n\n boundary near 100, so hard cap.
    assert out.endswith("…(truncated)")


# --- load_pa_context -------------------------------------------------------


def test_load_pa_context_full(fake_cc: Path):
    ctx = load_pa_context(fake_cc)
    assert "ABOUT BEN" in ctx
    assert "Founder of 7 ventures" in ctx["ABOUT BEN"]
    assert "BUSINESSES" in ctx
    assert "VOICE" in ctx
    assert "RECENT MEMORY" in ctx
    # Most-recent dated entries should be present
    assert "PA build kicked off" in ctx["RECENT MEMORY"]


def test_load_pa_context_handles_missing_dir():
    """Bad path returns empty dict, doesn't crash."""
    assert load_pa_context("/nonexistent/path/somewhere") == {}


def test_load_pa_context_handles_none():
    assert load_pa_context(None) == {}


def test_load_pa_context_skips_missing_optional_files(tmp_path: Path):
    """If only about-me exists, only that section appears."""
    (tmp_path / "_context").mkdir()
    (tmp_path / "_context" / "about-me.md").write_text("hi", encoding="utf-8")
    ctx = load_pa_context(tmp_path)
    assert ctx == {"ABOUT BEN": "hi"}


def test_load_pa_context_skips_empty_file(tmp_path: Path):
    """An empty file produces no section (no key in dict)."""
    (tmp_path / "_context").mkdir()
    (tmp_path / "_context" / "about-me.md").write_text("", encoding="utf-8")
    (tmp_path / "_context" / "voice.md").write_text("real content", encoding="utf-8")
    ctx = load_pa_context(tmp_path)
    assert "ABOUT BEN" not in ctx
    assert ctx.get("VOICE") == "real content"


# --- build_ask_prompt -------------------------------------------------------


def test_build_ask_prompt_includes_question_at_end():
    prompt = build_ask_prompt("what's the meaning of life?", {})
    # Question should be the last non-empty line/section
    assert "what's the meaning of life?" in prompt
    assert prompt.rstrip().endswith("what's the meaning of life?")


def test_build_ask_prompt_includes_each_context_label():
    ctx = {
        "ABOUT BEN": "founder",
        "BUSINESSES": "the2357",
        "VOICE": "casual",
    }
    prompt = build_ask_prompt("hello?", ctx)
    assert "--- ABOUT BEN ---" in prompt
    assert "--- BUSINESSES ---" in prompt
    assert "--- VOICE ---" in prompt
    assert "--- QUESTION ---" in prompt
    assert "founder" in prompt
    assert "the2357" in prompt


def test_build_ask_prompt_no_context():
    """Empty context dict still produces a usable prompt."""
    prompt = build_ask_prompt("test", {})
    assert "--- QUESTION ---" in prompt
    assert "test" in prompt
    # No section headers in body
    assert "--- ABOUT BEN ---" not in prompt


def test_build_ask_prompt_strips_whitespace_around_question():
    prompt = build_ask_prompt("   what's up?   \n\n", {})
    # The strip happens in build_ask_prompt's question.strip()
    assert "what's up?" in prompt
    assert "   what's up?   " not in prompt

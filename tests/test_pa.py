"""Pure-function tests for pa.load_pa_context + build_ask_prompt."""
from pathlib import Path

import pytest

from pa_agent.pa import (
    _read_file_capped,
    append_note,
    build_ask_prompt,
    detect_template_files,
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


# --- A2: append_note --------------------------------------------------------


def test_today_notes_filename_format():
    from datetime import UTC, datetime  # noqa: PLC0415

    from pa_agent.pa import _today_notes_filename  # noqa: PLC0415
    fixed = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
    assert _today_notes_filename(fixed) == "notes-2026-05-07.md"


def test_append_note_creates_file_with_header(tmp_path: Path):
    target = append_note(tmp_path, "first note ever")
    assert target is not None
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    # First write should include the header
    assert "Quick notes" in content
    assert "first note ever" in content
    # Timestamp block format
    assert "## " in content


def test_append_note_appends_to_existing_file(tmp_path: Path):
    """Second call same day reuses the file + appends without re-headering."""
    t1 = append_note(tmp_path, "first")
    t2 = append_note(tmp_path, "second")
    assert t1 == t2
    content = t1.read_text(encoding="utf-8")
    assert "first" in content
    assert "second" in content
    # Header should appear exactly once
    assert content.count("Quick notes") == 1


def test_append_note_strips_input(tmp_path: Path):
    target = append_note(tmp_path, "   spaced text\n   ")
    assert target is not None
    content = target.read_text(encoding="utf-8")
    assert "spaced text" in content
    # No leading whitespace artifact
    assert "   spaced text" not in content


def test_append_note_empty_returns_none(tmp_path: Path):
    assert append_note(tmp_path, "") is None
    assert append_note(tmp_path, "   ") is None


def test_append_note_missing_path_returns_none():
    assert append_note(None, "x") is None
    assert append_note("/nonexistent/dir", "x") is None


def test_append_note_creates_inbox_dir(tmp_path: Path):
    """Even if _inbox/ doesn't exist, append_note creates it."""
    assert not (tmp_path / "_inbox").exists()
    target = append_note(tmp_path, "x")
    assert target is not None
    assert (tmp_path / "_inbox").is_dir()


def test_load_pa_context_includes_inbox_notes(tmp_path: Path):
    """A note made with append_note should appear in load_pa_context."""
    from pa_agent.pa import load_pa_context
    append_note(tmp_path, "test note for context loading")
    ctx = load_pa_context(tmp_path)
    assert "RECENT QUICK-NOTES" in ctx
    assert "test note for context loading" in ctx["RECENT QUICK-NOTES"]


# --- A6: detect_template_files ---------------------------------------------


def test_detect_templates_flags_template_marker(tmp_path: Path):
    (tmp_path / "_context").mkdir()
    (tmp_path / "_context" / "voice.md").write_text(
        "# Voice\n\nTEMPLATE — fill in\n", encoding="utf-8",
    )
    flagged = detect_template_files(tmp_path)
    assert "_context/voice.md" in flagged


def test_detect_templates_flags_short_files(tmp_path: Path):
    (tmp_path / "_context").mkdir()
    (tmp_path / "_context" / "people.md").write_text(
        "# People\n\nshort\n", encoding="utf-8",
    )
    flagged = detect_template_files(tmp_path)
    assert "_context/people.md" in flagged


def test_detect_templates_skips_filled_files(tmp_path: Path):
    (tmp_path / "_context").mkdir()
    real_content = "# About Ben\n\n" + ("Real biographical content. " * 20)
    (tmp_path / "_context" / "about-me.md").write_text(real_content, encoding="utf-8")
    flagged = detect_template_files(tmp_path)
    assert "_context/about-me.md" not in flagged


def test_detect_templates_skips_missing_files(tmp_path: Path):
    """Files that don't exist aren't flagged (different from being a template)."""
    flagged = detect_template_files(tmp_path)
    assert flagged == []


def test_detect_templates_handles_missing_path():
    assert detect_template_files(None) == []
    assert detect_template_files("/nonexistent") == []

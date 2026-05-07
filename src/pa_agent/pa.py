"""Personal-assistant Q&A — Phase 8 v0.3+v0.4.

Surfaces an /ask command that takes a free-form question, loads
CommandCenter context (about-me / about-businesses / voice / recent
memory + new: /_inbox/notes-YYYY-MM-DD.md daily quick-notes), and
returns an LLM-grounded answer.

v0.4 adds /note — appends a timestamped line to today's
`_inbox/notes-YYYY-MM-DD.md` so Ben can capture thoughts via Telegram in
real time. Notes are immediately picked up by load_pa_context() so the
next /ask sees them. Mac canonical memory.md is untouched (no git push
from ai-primary) — Ben manually merges notes into memory.md from Mac
when convenient.

Pure helpers (`load_pa_context`, `build_ask_prompt`, `append_note`,
`_today_notes_filename`) are unit-testable. The async `answer_question`
composes I/O around them.
"""
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from openai import AsyncOpenAI

from pa_agent.settings import settings

log = logging.getLogger(__name__)


# Files we pull from CommandCenter for grounding the PA. Each is optional
# — missing files just produce an empty section (no crash).
_CONTEXT_FILES: list[tuple[str, str]] = [
    # (display label, relative path under commandcenter_path)
    ("ABOUT BEN", "_context/about-me.md"),
    ("BUSINESSES", "_context/about-businesses.md"),
    ("VOICE", "_context/voice.md"),
    ("PEOPLE", "_context/people.md"),
    ("SYSTEM POLICY", "_system/policy.md"),
]

# Cap each context section so total tokens stay sane on small models.
_PER_FILE_CHAR_CAP = 4_000
_RECENT_MEMORY_CHAR_CAP = 6_000


def _read_file_capped(p: Path, cap: int) -> str:
    """Read a markdown file, return first `cap` chars (or empty if missing)."""
    if not p.exists() or not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    if len(text) <= cap:
        return text
    # Cut at a paragraph boundary near the cap to avoid mid-sentence ellipses.
    cut = text.rfind("\n\n", 0, cap)
    if cut < cap // 2:
        cut = cap
    return text[:cut].rstrip() + "\n\n…(truncated)"


def _load_recent_memory(path: Path, limit: int = 5) -> str:
    """Last `limit` dated entries from `_system/memory.md`.

    Mirrors brief._load_commandcenter_memory for consistency.
    """
    memory_file = path / "_system" / "memory.md"
    if not memory_file.exists():
        return ""
    try:
        content = memory_file.read_text(encoding="utf-8")
    except Exception:
        return ""
    parts = re.split(r"(?m)^(## \d{4}-\d{2}-\d{2}.*)$", content)
    entries: list[str] = []
    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        entries.append(f"{header}\n{body}".strip())
    if not entries:
        return ""
    joined = "\n\n".join(entries[-limit:])
    if len(joined) > _RECENT_MEMORY_CHAR_CAP:
        joined = joined[-_RECENT_MEMORY_CHAR_CAP:]
        joined = "…(truncated)\n" + joined
    return joined


def _today_notes_filename(d: datetime | None = None) -> str:
    """Pure: return the inbox notes filename for a given date (UTC).

    Format `notes-YYYY-MM-DD.md`. Each day gets its own file so it's
    easy to merge into memory.md per-day on Mac.
    """
    when = d or datetime.now(UTC)
    return f"notes-{when:%Y-%m-%d}.md"


def _load_recent_inbox_notes(path: Path, days: int = 3) -> str:
    """Read up to `days` most-recent _inbox/notes-*.md files, newest first."""
    inbox = path / "_inbox"
    if not inbox.exists() or not inbox.is_dir():
        return ""
    files = sorted(inbox.glob("notes-*.md"), reverse=True)[:days]
    if not files:
        return ""
    sections: list[str] = []
    total = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not content:
            continue
        section = f"--- {f.name} ---\n{content}"
        total += len(section)
        sections.append(section)
        if total > _RECENT_MEMORY_CHAR_CAP:
            break
    return "\n\n".join(sections)


def append_note(path: Path | str | None, text: str) -> Path | None:
    """Pure-ish: append a timestamped note to today's inbox file.

    Creates the `_inbox/` directory if absent. Returns the absolute path
    of the file written, or None if path is invalid / write fails.

    Format per line:
      ## HH:MM:SS UTC
      <text>
      \n

    Easy to grep, easy to merge into memory.md by hand.
    """
    if not text or not text.strip():
        return None
    if path is None:
        return None
    path_obj = Path(path) if isinstance(path, str) else path
    if not path_obj.exists():
        return None
    inbox = path_obj / "_inbox"
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    target = inbox / _today_notes_filename()
    now = datetime.now(UTC)
    block = f"## {now:%H:%M:%S UTC}\n{text.strip()}\n\n"
    try:
        # Touch file with header on first write of the day.
        if not target.exists():
            header = (
                f"# Quick notes — {now:%Y-%m-%d}\n\n"
                "Captured via PA /note. Merge into _system/memory.md "
                "when convenient.\n\n"
            )
            target.write_text(header, encoding="utf-8")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(block)
    except Exception:
        return None
    return target


def load_pa_context(path: Path | str | None) -> dict[str, str]:
    """Pure: read CommandCenter context files + recent memory + recent
    inbox notes into a dict.

    Returns {label: content} for every section that has non-empty content.
    Missing files produce no entry. Used by build_ask_prompt() and tests.
    """
    if path is None:
        return {}
    path_obj = Path(path) if isinstance(path, str) else path
    if not path_obj.exists():
        return {}

    out: dict[str, str] = {}
    for label, rel in _CONTEXT_FILES:
        content = _read_file_capped(path_obj / rel, _PER_FILE_CHAR_CAP)
        if content.strip():
            out[label] = content

    recent = _load_recent_memory(
        path_obj, limit=settings.commandcenter_memory_entries
    )
    if recent:
        out["RECENT MEMORY"] = recent

    inbox_notes = _load_recent_inbox_notes(path_obj, days=3)
    if inbox_notes:
        out["RECENT QUICK-NOTES"] = inbox_notes
    return out


def build_ask_prompt(question: str, context: dict[str, str]) -> str:
    """Pure: compose the prompt sent to the LLM. Tests can assert structure."""
    lines: list[str] = [
        "You are Ben's personal assistant — a workplace-collaborator-style "
        "helper. You have privileged access to Ben's CommandCenter (his "
        "personal-AI-OS workspace). Answer his question grounded in the "
        "context below. Be direct, short, and useful — a smart colleague, "
        "not a chatbot. Quote specific facts from the context when relevant. "
        "If the context doesn't cover the question, say so plainly.",
        "",
        "Tone: match the VOICE section if present. Otherwise: thoughtful, "
        "concise, no marketing-speak, no ending pleasantries.",
        "",
        "Format: plain text or simple Telegram-friendly HTML (<b>, <i>, "
        "<code>). No code-fenced blocks. Keep response under ~1500 chars "
        "unless the question needs longer.",
        "",
    ]
    for label, content in context.items():
        lines.append(f"--- {label} ---")
        lines.append(content)
        lines.append("")
    lines.append("--- QUESTION ---")
    lines.append(question.strip())
    return "\n".join(lines)


def detect_template_files(path: Path | str | None) -> list[str]:
    """Pure: identify CommandCenter context files that are still templates.

    A "template" is a file that exists but contains the literal string
    "TEMPLATE" or "fill in" or has fewer than 200 chars of real content.
    Returns a list of human-readable filenames the user should fill.

    Used by /ask to nudge the user when their PA grounding is thin.
    """
    if path is None:
        return []
    path_obj = Path(path) if isinstance(path, str) else path
    if not path_obj.exists():
        return []
    template_markers = ("TEMPLATE", "template — fill", "fill in", "<fill>")
    out: list[str] = []
    for label, rel in _CONTEXT_FILES:
        f = path_obj / rel
        if not f.exists() or not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # Strip code fences + headers to count actual content
        meaningful = re.sub(r"^#.*$", "", content, flags=re.M).strip()
        if any(marker in content for marker in template_markers):
            out.append(rel)
        elif len(meaningful) < 200:
            out.append(rel)
    return out


async def answer_question(question: str) -> str:
    """Compose context + send to LiteLLM. Returns the answer text."""
    if not question.strip():
        return "Ask me something. Try: /ask what's on my plate this week"
    if not settings.litellm_api_key:
        return "❌ LLM not configured (LITELLM_API_KEY missing)"

    context = load_pa_context(settings.commandcenter_path)
    prompt = build_ask_prompt(question, context)

    try:
        client = AsyncOpenAI(
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
        )
        resp = await client.chat.completions.create(
            model=settings.litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=600,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            answer = "(empty response from LLM)"

        # A6: nudge about template files (max once per /ask, low-friction)
        templates = detect_template_files(settings.commandcenter_path)
        if templates:
            answer += (
                "\n\n<i>(grounding is thin — these context files are still "
                f"templates: {', '.join(templates)}. Fill them on Mac to "
                "improve future /ask answers.)</i>"
            )
        return answer
    except Exception:
        log.exception("PA /ask failed for q=%r", question[:80])
        return "❌ PA /ask failed — check logs"

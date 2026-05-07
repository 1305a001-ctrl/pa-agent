"""Personal-assistant Q&A — Phase 8 v0.3 (PA build kickoff).

Surfaces an /ask command that takes a free-form question, loads
CommandCenter context (about-me / about-businesses / voice / recent
memory), and returns an LLM-grounded answer.

The PA is meant to be a workplace-collaborator-style helper: it knows
who Ben is, what his businesses are, what he's been working on
recently, and how he prefers to be talked to. Trading-specific
queries already go through /status + /brief; this is for everything else
(meetings, decisions, recall, drafting).

Pure helpers (`load_pa_context`, `build_ask_prompt`) are unit-testable.
The async `answer_question` composes I/O around them.
"""
import logging
import re
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


def load_pa_context(path: Path | str | None) -> dict[str, str]:
    """Pure: read CommandCenter context files + recent memory into a dict.

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
        return answer or "(empty response from LLM)"
    except Exception:
        log.exception("PA /ask failed for q=%r", question[:80])
        return "❌ PA /ask failed — check logs"

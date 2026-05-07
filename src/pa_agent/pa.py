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
import json
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


async def triage_inbox_text(raw_text: str) -> dict[str, str]:
    """Pure-ish: hand a chunk of forwarded email/whatsapp/etc text to the
    LLM for triage. Returns a dict with:
        summary       — 1 sentence
        action_items  — bullet list, can be empty
        urgency       — low | medium | high
        category      — one of: business, personal, financial, social, spam, other
        raw_excerpt   — first 200 chars of input for the audit log

    Called by /inbox handler. Result gets appended to
    `_inbox/triage-YYYY-MM-DD.md` so Ben can batch-process later, plus
    the summary is sent back to Telegram.

    On LLM error returns a minimal stub with summary='LLM unavailable'.
    """
    raw_text = (raw_text or "").strip()
    excerpt = raw_text[:200].replace("\n", " ")
    if not raw_text:
        return {
            "summary": "(empty input)", "action_items": "",
            "urgency": "low", "category": "other", "raw_excerpt": "",
        }
    if not settings.litellm_api_key:
        return {
            "summary": "LLM unavailable", "action_items": "",
            "urgency": "low", "category": "other", "raw_excerpt": excerpt,
        }
    prompt = (
        "Triage this forwarded message for Ben (the system owner). Output "
        "ONLY raw JSON (no code fences) with these keys:\n"
        "  summary: a single short sentence describing what it's about\n"
        "  action_items: one-line bullets of things Ben needs to do "
        "(e.g. '- reply to John by Friday'). Empty string if none.\n"
        "  urgency: 'low', 'medium', or 'high'\n"
        "  category: 'business', 'personal', 'financial', 'social', 'spam', or 'other'\n"
        "\n"
        "Forwarded message:\n---\n"
        f"{raw_text}\n---"
    )
    try:
        client = AsyncOpenAI(
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
        )
        resp = await client.chat.completions.create(
            model=settings.litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400,
        )
        body = (resp.choices[0].message.content or "").strip()
        # Strip code fences if the model included them despite instructions.
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
        parsed = json.loads(body)
        # Normalize keys, fill missing.
        return {
            "summary": str(parsed.get("summary", "(no summary)"))[:500],
            "action_items": str(parsed.get("action_items", "")).strip()[:1000],
            "urgency": str(parsed.get("urgency", "low")).lower(),
            "category": str(parsed.get("category", "other")).lower(),
            "raw_excerpt": excerpt,
        }
    except Exception:
        log.exception("triage_inbox_text failed")
        return {
            "summary": "(LLM error — see logs)",
            "action_items": "", "urgency": "low",
            "category": "other", "raw_excerpt": excerpt,
        }


def append_triage(
    path: Path | str | None, raw_text: str, triage: dict[str, str]
) -> Path | None:
    """Append a triage record to `_inbox/triage-YYYY-MM-DD.md`. Returns
    target path or None on failure.
    """
    if path is None or not raw_text.strip():
        return None
    path_obj = Path(path) if isinstance(path, str) else path
    if not path_obj.exists():
        return None
    inbox = path_obj / "_inbox"
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    now = datetime.now(UTC)
    target = inbox / f"triage-{now:%Y-%m-%d}.md"
    block = (
        f"## {now:%H:%M:%S UTC} — [{triage.get('urgency', '?')}/"
        f"{triage.get('category', '?')}]\n"
        f"**Summary:** {triage.get('summary', '')}\n"
    )
    actions = triage.get("action_items", "").strip()
    if actions:
        block += f"**Action items:**\n{actions}\n"
    block += f"<details>\n<summary>Original text</summary>\n\n```\n{raw_text}\n```\n</details>\n\n"
    try:
        if not target.exists():
            target.write_text(
                f"# Triage queue — {now:%Y-%m-%d}\n\n"
                "Forwarded items captured via /inbox. Process when convenient.\n\n",
                encoding="utf-8",
            )
        with target.open("a", encoding="utf-8") as fh:
            fh.write(block)
    except Exception:
        return None
    return target


def load_calendar_today(path: Path | str | None) -> str:
    """Pure: read CommandCenter `_inbox/calendar.md` if present and return
    the section for today's date. Format expected:

      ## 2026-05-08
      - 09:00 Team standup
      - 14:00 Pickle padel with Adi
      - 19:00 Sermon prep call

      ## 2026-05-09
      ...

    Returns "" if file missing / today not in file. Used by Option B —
    daily brief calendar awareness. No live calendar API integration; Ben
    drops a markdown file when he wants the PA to know.
    """
    if path is None:
        return ""
    path_obj = Path(path) if isinstance(path, str) else path
    cal_file = path_obj / "_inbox" / "calendar.md"
    if not cal_file.exists():
        return ""
    try:
        content = cal_file.read_text(encoding="utf-8")
    except Exception:
        return ""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # Find "## YYYY-MM-DD" header for today; capture until next "## " or EOF.
    pattern = rf"(?ms)^## {re.escape(today)}.*?(?=^## \d{{4}}-\d{{2}}-\d{{2}}|\Z)"
    match = re.search(pattern, content)
    if not match:
        return ""
    block = match.group(0).strip()
    # Drop the header line itself; return just the body (bullets etc.)
    body = re.sub(rf"^## {re.escape(today)}.*?\n", "", block, count=1).strip()
    return body


_CHAT_HISTORY_KEY = "pa:chat_history"
_CHAT_HISTORY_MAX = 6  # last N exchanges (user + assistant pairs)


async def load_chat_history(redis_client) -> list[dict[str, str]]:
    """Pure-ish: pull last N message pairs from redis. Returns list of
    {role: 'user'|'assistant', content: str} dicts in chronological order.

    Used by answer_question() to thread /ask context across messages.
    Storage is a redis LIST `pa:chat_history` of JSON strings.
    """
    if redis_client is None:
        return []
    try:
        raw = await redis_client.lrange(_CHAT_HISTORY_KEY, 0, _CHAT_HISTORY_MAX * 2 - 1)
    except Exception:
        log.exception("load_chat_history failed")
        return []
    out: list[dict[str, str]] = []
    for entry in reversed(raw):  # oldest first
        try:
            decoded = json.loads(entry) if isinstance(entry, str) else entry
            if isinstance(decoded, dict) and "role" in decoded and "content" in decoded:
                out.append(decoded)
        except (json.JSONDecodeError, TypeError):
            continue
    return out


async def append_chat_history(
    redis_client, user_msg: str, assistant_msg: str
) -> None:
    """Append one user + one assistant message to history; trim to limit."""
    if redis_client is None:
        return
    try:
        # LPUSH adds to head; we want most-recent first, oldest last.
        # Then LTRIM to keep only the last N pairs.
        await redis_client.lpush(
            _CHAT_HISTORY_KEY,
            json.dumps({"role": "assistant", "content": assistant_msg}),
            json.dumps({"role": "user", "content": user_msg}),
        )
        await redis_client.ltrim(_CHAT_HISTORY_KEY, 0, _CHAT_HISTORY_MAX * 2 - 1)
    except Exception:
        log.exception("append_chat_history failed")


async def reset_chat_history(redis_client) -> None:
    """Drop all stored chat history. Used by /reset command."""
    if redis_client is None:
        return
    try:
        await redis_client.delete(_CHAT_HISTORY_KEY)
    except Exception:
        log.exception("reset_chat_history failed")


async def load_trading_snapshot(db) -> str:
    """Pure-ish: query open positions + recent fills + DD from DB; render
    a compact text block suitable for /ask context.

    Used by answer_question() to inject current trading state into prompts
    so the PA can answer questions like "should I close my MSTR short?"
    or "how's my portfolio doing right now?".

    Returns "" on DB error / no data.
    """
    try:
        # Open positions across all venues
        open_rows = await db.pool.fetch(
            """
            SELECT venue, asset, side, qty, avg_entry_price, mark_price,
                   unrealized_pnl_usd, opened_at
            FROM positions
            WHERE status = 'open' AND qty > 0
            ORDER BY (qty * COALESCE(mark_price, avg_entry_price)) DESC
            LIMIT 20
            """
        )
        # Recent closed positions (last 24h)
        closed_rows = await db.pool.fetch(
            """
            SELECT venue, asset, side, realized_pnl_usd, closed_at
            FROM positions
            WHERE status = 'closed'
              AND closed_at >= NOW() - INTERVAL '24 hours'
              AND COALESCE(realized_pnl_usd, 0) <> 0
            ORDER BY closed_at DESC
            LIMIT 10
            """
        )
        # Latest risk snapshot
        risk_row = await db.pool.fetchrow(
            """
            SELECT pnl_usd, drawdown_pct, exposure_usd
            FROM risk_ledger
            WHERE scope = 'total' AND period = 'daily'
            ORDER BY snapshot_at DESC LIMIT 1
            """
        )
    except Exception:
        log.exception("load_trading_snapshot failed")
        return ""

    lines: list[str] = []
    if risk_row:
        lines.append(
            f"Daily PnL: ${float(risk_row['pnl_usd']):+.2f} · "
            f"DD: {float(risk_row['drawdown_pct']):.2f}% · "
            f"Open exposure: ${float(risk_row['exposure_usd']):,.0f}"
        )
        lines.append("")
    if open_rows:
        lines.append(f"Open positions ({len(open_rows)}):")
        for r in open_rows:
            mark = r["mark_price"] or r["avg_entry_price"]
            notional = float(r["qty"]) * float(mark or 0)
            upnl = r["unrealized_pnl_usd"]
            upnl_str = f"{float(upnl):+.2f}" if upnl is not None else "—"
            asset = str(r["asset"])
            if len(asset) > 40:
                asset = asset[:37] + "…"
            lines.append(
                f"  {r['venue']:>10} {asset:<42} {r['side']:>5} "
                f"${notional:>8,.0f} unreal=${upnl_str}"
            )
        lines.append("")
    if closed_rows:
        lines.append(f"Recently closed (24h, last {len(closed_rows)}):")
        for r in closed_rows:
            asset = str(r["asset"])
            if len(asset) > 40:
                asset = asset[:37] + "…"
            lines.append(
                f"  {r['venue']:>10} {asset:<42} "
                f"{r['side']:>5} pnl=${float(r['realized_pnl_usd']):+.2f}"
            )
    if not (open_rows or closed_rows or risk_row):
        return ""
    return "\n".join(lines)


def build_me_summary(context: dict[str, str]) -> str:
    """Pure: render a compact "what the PA knows about you" summary.

    Used by the /me Telegram command. Surfaces:
      - Whether each context section is filled (✅ or 🟡 template)
      - Char count per section as a rough density signal
      - Most recent dated memory entry
      - Any recent quick-notes

    HTML-formatted for Telegram (<b>, <i>, <code>).
    """
    lines: list[str] = ["<b>What I know about you</b>", ""]
    section_health = []
    for label, _ in _CONTEXT_FILES:
        present = context.get(label, "")
        # Heuristic: marked as template if contains "TEMPLATE" or under 200 chars
        is_template = (
            "TEMPLATE" in present
            or "fill in" in present.lower()
            or len(re.sub(r"^#.*$", "", present, flags=re.M).strip()) < 200
        )
        marker = "🟡 thin" if is_template or not present else "✅"
        chars = len(present)
        section_health.append(
            f"  {marker} <b>{label.title()}</b> "
            f"<i>({chars:,} chars)</i>"
        )
    lines.extend(section_health)

    recent = context.get("RECENT MEMORY", "").strip()
    if recent:
        lines.append("")
        lines.append("<b>Most recent memory entry:</b>")
        # _load_recent_memory joins entries oldest-first; the LAST entry is
        # the most recent. Split on `## YYYY-MM-DD` headers to get clean
        # entry boundaries (bodies often contain `\n\n` paragraphs).
        entry_starts = list(
            re.finditer(r"^## \d{4}-\d{2}-\d{2}.*$", recent, re.MULTILINE)
        )
        if entry_starts:
            last_start = entry_starts[-1].start()
            latest_block = recent[last_start:].strip()
        else:
            latest_block = recent
        snippet = latest_block[:300]
        if len(latest_block) > 300:
            snippet += "…"
        lines.append(f"<i>{snippet}</i>")

    notes = context.get("RECENT QUICK-NOTES", "").strip()
    if notes:
        lines.append("")
        # Count number of quick-notes (each starts with ## HH:MM:SS UTC)
        n_notes = len(re.findall(r"^## \d{2}:\d{2}:\d{2}", notes, re.M))
        lines.append(f"<b>Recent quick-notes:</b> {n_notes} captured")

    if not recent and not notes:
        lines.append("")
        lines.append(
            "<i>No memory entries or notes yet. Use /note &lt;text&gt; to "
            "capture thoughts; they're loaded into the next /ask immediately.</i>"
        )
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


async def answer_question(question: str, db=None, redis_client=None) -> str:
    """Compose context + send to LiteLLM. Returns the answer text.

    Args:
      question: the user's /ask text.
      db: pa_agent.db.DB instance — if passed, injects current TRADING NOW
          state into context (open positions, recent fills, daily PnL/DD).
      redis_client: aioredis instance — if passed, threads multi-turn
          conversation context across /ask invocations (last 6 exchanges).
    """
    if not question.strip():
        return "Ask me something. Try: /ask what's on my plate this week"
    if not settings.litellm_api_key:
        return "❌ LLM not configured (LITELLM_API_KEY missing)"

    context = load_pa_context(settings.commandcenter_path)
    if db is not None:
        try:
            snapshot = await load_trading_snapshot(db)
            if snapshot:
                context["TRADING NOW"] = snapshot
        except Exception:
            log.exception("trading-snapshot injection failed (non-fatal)")

    # A3: multi-turn — pull prior exchanges from redis if available.
    history: list[dict[str, str]] = []
    if redis_client is not None:
        try:
            history = await load_chat_history(redis_client)
        except Exception:
            log.exception("chat history load failed (non-fatal)")

    system_prompt = build_ask_prompt(question, context)
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    # Drop the QUESTION from system prompt; thread it as the latest user msg
    # alongside the prior exchanges. This way the LLM sees: system context →
    # past conversation → current question.
    if history:
        # Detect: if the question is short (likely a follow-up like "more
        # detail" / "what about X"), threading helps. If it's clearly a
        # standalone (long question with explicit context), prior history
        # is just noise. Heuristic: include history when question < 150 chars.
        if len(question) < 150:
            messages.extend(history)

    messages.append({"role": "user", "content": question.strip()})

    try:
        client = AsyncOpenAI(
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
        )
        resp = await client.chat.completions.create(
            model=settings.litellm_model,
            # OpenAI types `messages` as a long union of TypedDicts; our
            # `list[dict[str,str]]` shape is structurally equivalent. The
            # type-ignore avoids a forced refactor to ChatCompletionMessageParam.
            messages=messages,  # type: ignore[arg-type]
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

        # A3: persist this exchange so the next /ask can thread off it
        if redis_client is not None:
            try:
                await append_chat_history(redis_client, question, answer)
            except Exception:
                log.exception("chat history append failed (non-fatal)")

        return answer
    except Exception:
        log.exception("PA /ask failed for q=%r", question[:80])
        return "❌ PA /ask failed — check logs"

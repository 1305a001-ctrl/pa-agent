"""Tests for the inbox auto-pull loop — pure helpers + tick behaviour with
mocked Gmail client + LLM."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pa_agent.gmail import GmailMessage
from pa_agent.inbox_loop import (
    _process_one_message,
    _should_forward_to_telegram,
    _tick,
    format_triage_for_telegram,
)
from pa_agent.settings import settings

# --- _should_forward_to_telegram ---

class TestShouldForwardToTelegram:
    def test_high_meets_high_threshold(self):
        with patch.object(settings, "gmail_telegram_min_urgency", "high"):
            assert _should_forward_to_telegram("high")

    def test_medium_does_not_meet_high_threshold(self):
        with patch.object(settings, "gmail_telegram_min_urgency", "high"):
            assert not _should_forward_to_telegram("medium")

    def test_low_does_not_meet_high_threshold(self):
        with patch.object(settings, "gmail_telegram_min_urgency", "high"):
            assert not _should_forward_to_telegram("low")

    def test_medium_meets_medium_threshold(self):
        with patch.object(settings, "gmail_telegram_min_urgency", "medium"):
            assert _should_forward_to_telegram("medium")
            assert _should_forward_to_telegram("high")
            assert not _should_forward_to_telegram("low")

    def test_low_threshold_passes_everything(self):
        with patch.object(settings, "gmail_telegram_min_urgency", "low"):
            assert _should_forward_to_telegram("low")
            assert _should_forward_to_telegram("medium")
            assert _should_forward_to_telegram("high")

    def test_unknown_urgency_treated_as_low(self):
        with patch.object(settings, "gmail_telegram_min_urgency", "high"):
            assert not _should_forward_to_telegram("unknown")
            assert not _should_forward_to_telegram("")


# --- format_triage_for_telegram ---

class TestFormatTriageForTelegram:
    def _msg(self) -> GmailMessage:
        return GmailMessage(
            id="m1", thread_id="t1",
            sender="John <john@acme.com>",
            subject="Q3 budget review",
            snippet="snip",
            body_text="body",
            received_unix=1700000000,
        )

    def test_high_urgency_formatting(self):
        triage = {
            "urgency": "high", "category": "business",
            "summary": "Q3 budget review needs sign-off by Friday.",
            "action_items": "- reply by Friday",
        }
        out = format_triage_for_telegram(self._msg(), triage)
        assert "🚨" in out
        assert "Q3 budget review" in out
        assert "john@acme.com" in out
        assert "reply by Friday" in out

    def test_medium_urgency_uses_yellow(self):
        triage = {"urgency": "medium", "summary": "FYI", "action_items": ""}
        out = format_triage_for_telegram(self._msg(), triage)
        assert "🟡" in out

    def test_low_urgency_uses_default_icon(self):
        triage = {"urgency": "low", "summary": "FYI", "action_items": ""}
        out = format_triage_for_telegram(self._msg(), triage)
        assert "📨" in out

    def test_html_special_chars_escaped(self):
        msg = GmailMessage(
            id="m2", thread_id="t2",
            sender="<test>", subject="A & B <c>",
            snippet="", body_text="",
            received_unix=0,
        )
        triage = {"urgency": "low", "summary": "x < y", "action_items": ""}
        out = format_triage_for_telegram(msg, triage)
        # Reserved chars escaped to entities so Telegram HTML mode doesn't choke.
        assert "&lt;" in out
        assert "&amp;" in out

    def test_empty_action_items_omits_section(self):
        triage = {"urgency": "low", "summary": "ok", "action_items": ""}
        out = format_triage_for_telegram(self._msg(), triage)
        assert "Actions" not in out


# --- _tick when credentials missing ---

@pytest.mark.asyncio
async def test_tick_returns_zero_when_credentials_absent():
    with patch.object(settings, "gmail_oauth_client_id", ""):
        result = await _tick()
        assert result == 0


# --- _process_one_message integration ---

@pytest.mark.asyncio
async def test_process_one_message_archives_and_marks_read():
    """Smoke: triage runs → archive runs → mark-as-read runs. With LLM
    unavailable (no API key), triage_inbox_text returns its safe stub."""
    msg = GmailMessage(
        id="m1", thread_id="t1",
        sender="bot@example.com", subject="hi", snippet="", body_text="hello",
        received_unix=0,
    )

    mark_read_mock = AsyncMock()
    telegram_mock = AsyncMock()

    with patch("pa_agent.inbox_loop.append_triage", return_value=None) as appended, \
         patch("pa_agent.inbox_loop.gmail_client") as mock_gmail, \
         patch("pa_agent.inbox_loop.alerts") as mock_alerts, \
         patch("pa_agent.inbox_loop.triage_inbox_text", new=AsyncMock(return_value={
             "summary": "test", "urgency": "low",
             "category": "other", "action_items": "", "raw_excerpt": "",
         })):
        mock_gmail.mark_as_read = mark_read_mock
        mock_alerts.telegram = telegram_mock

        await _process_one_message(msg)

        appended.assert_called_once()
        mark_read_mock.assert_awaited_once_with("m1")
        # Low urgency + default 'high' threshold → no telegram push.
        telegram_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_message_high_urgency_forwards_to_telegram():
    msg = GmailMessage(
        id="m1", thread_id="t1",
        sender="ceo@example.com", subject="urgent", snippet="", body_text="x",
        received_unix=0,
    )

    telegram_mock = AsyncMock()
    mark_read_mock = AsyncMock()
    with patch("pa_agent.inbox_loop.append_triage", return_value=None), \
         patch("pa_agent.inbox_loop.gmail_client") as mock_gmail, \
         patch("pa_agent.inbox_loop.alerts") as mock_alerts, \
         patch("pa_agent.inbox_loop.triage_inbox_text", new=AsyncMock(return_value={
             "summary": "Critical issue", "urgency": "high",
             "category": "business", "action_items": "- reply immediately", "raw_excerpt": "",
         })), \
         patch.object(settings, "gmail_telegram_min_urgency", "high"):
        mock_gmail.mark_as_read = mark_read_mock
        mock_alerts.telegram = telegram_mock

        await _process_one_message(msg)

        # high urgency triggers telegram + mark-as-read.
        telegram_mock.assert_awaited_once()
        mark_read_mock.assert_awaited_once_with("m1")
        # The pushed body should contain key fields.
        push_text = telegram_mock.await_args.args[0]
        assert "Critical issue" in push_text

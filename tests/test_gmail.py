"""Tests for the Gmail client — pure parsing logic + dormant-when-unconfigured."""
from __future__ import annotations

import base64
from unittest.mock import patch

from pa_agent.gmail import (
    _credentials_present,
    _decode_b64url,
    _strip_html,
    _walk_payload_for_text,
    parse_message_payload,
)
from pa_agent.settings import settings

# --- Pure helpers ---

class TestDecodeB64Url:
    def test_decodes_no_padding(self):
        # "Hello, world!" — Gmail strips trailing '=' padding sometimes.
        encoded = base64.urlsafe_b64encode(b"Hello, world!").rstrip(b"=").decode()
        assert _decode_b64url(encoded) == "Hello, world!"

    def test_decodes_with_padding(self):
        encoded = base64.urlsafe_b64encode(b"abc").decode()  # has '=' padding
        assert _decode_b64url(encoded) == "abc"

    def test_unicode(self):
        encoded = base64.urlsafe_b64encode("héllo 🌎".encode()).rstrip(b"=").decode()
        assert _decode_b64url(encoded) == "héllo 🌎"

    def test_garbage_returns_empty_string(self):
        assert _decode_b64url("not-valid-base64-!!!!") == ""


class TestStripHtml:
    def test_strips_tags(self):
        assert _strip_html("<p>Hello <b>world</b>!</p>") == "Hello world!"

    def test_strips_nested(self):
        assert _strip_html("<div><a href='x'>Link</a> text</div>") == "Link text"

    def test_no_tags_passthrough(self):
        assert _strip_html("just text") == "just text"

    def test_strips_self_closing(self):
        assert _strip_html("line1<br/>line2") == "line1line2"


class TestWalkPayloadForText:
    def test_finds_top_level_text_plain(self):
        body = base64.urlsafe_b64encode(b"plain body").rstrip(b"=").decode()
        payload = {
            "mimeType": "text/plain",
            "body": {"data": body},
        }
        assert _walk_payload_for_text(payload) == "plain body"

    def test_descends_into_parts(self):
        body = base64.urlsafe_b64encode(b"nested body").rstrip(b"=").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": body}},
            ],
        }
        assert _walk_payload_for_text(payload) == "nested body"

    def test_prefers_text_plain_when_both_present(self):
        text_plain = base64.urlsafe_b64encode(b"plain wins").rstrip(b"=").decode()
        text_html = base64.urlsafe_b64encode(b"<p>html</p>").rstrip(b"=").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": text_plain}},
                {"mimeType": "text/html", "body": {"data": text_html}},
            ],
        }
        assert _walk_payload_for_text(payload) == "plain wins"

    def test_falls_back_to_html(self):
        text_html = base64.urlsafe_b64encode(b"<p>only html</p>").rstrip(b"=").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": text_html}},
            ],
        }
        assert _walk_payload_for_text(payload) == "only html"

    def test_returns_none_when_no_text(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "image/jpeg", "body": {"attachmentId": "x"}},
            ],
        }
        assert _walk_payload_for_text(payload) is None


class TestParseMessagePayload:
    def test_extracts_subject_and_body(self):
        body = base64.urlsafe_b64encode(b"the body").rstrip(b"=").decode()
        payload = {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Hello from Acme"},
                {"name": "From", "value": "john@acme.com"},
            ],
            "body": {"data": body},
        }
        subj, txt = parse_message_payload(payload)
        assert subj == "Hello from Acme"
        assert txt == "the body"

    def test_default_subject(self):
        payload = {
            "mimeType": "text/plain",
            "headers": [],
            "body": {},
            "snippet": "fallback snippet",
        }
        subj, txt = parse_message_payload(payload)
        assert subj == "(no subject)"
        assert txt == "fallback snippet"

    def test_case_insensitive_headers(self):
        # Gmail returns headers with capitalised names; some intermediaries normalise.
        payload = {
            "mimeType": "text/plain",
            "headers": [
                {"name": "subject", "value": "lowercase header name"},
            ],
            "body": {},
            "snippet": "x",
        }
        subj, _ = parse_message_payload(payload)
        assert subj == "lowercase header name"


# --- Settings gate ---

class TestCredentialsPresent:
    def test_all_three_required(self):
        with patch.object(settings, "gmail_oauth_client_id", "x"), \
             patch.object(settings, "gmail_oauth_client_secret", ""), \
             patch.object(settings, "gmail_oauth_refresh_token", "z"):
            assert not _credentials_present()

    def test_all_present(self):
        with patch.object(settings, "gmail_oauth_client_id", "x"), \
             patch.object(settings, "gmail_oauth_client_secret", "y"), \
             patch.object(settings, "gmail_oauth_refresh_token", "z"):
            assert _credentials_present()

    def test_default_dormant(self):
        # Defaults are empty strings — loop should be dormant by default.
        # (Test isolated by checking actual settings instance)
        # We can't assume what's in settings during tests; just verify the helper
        # honours the all-or-nothing rule when any field is empty.
        with patch.object(settings, "gmail_oauth_client_id", ""), \
             patch.object(settings, "gmail_oauth_client_secret", "y"), \
             patch.object(settings, "gmail_oauth_refresh_token", "z"):
            assert not _credentials_present()

"""Tests for the pure functions in telegram_digest.py (no network, no DB)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import telegram_digest as td


ROW = {
    "id": "11111111-2222-3333-4444-555555555555",
    "org": "Example Org",
    "title": "Senior Advisor <& Co>",
    "llm_score": 89,
    "llm_summary": "A short vacancy summary.",
    "full_description": None,
    "snippet": None,
    "compensation": None,
    "locations": [{
        "url": "https://example.com/jobs/1?a=1&b=2",
        "city": "London", "country": "UK", "region": "europe",
        "work_mode": "hybrid", "compensation": "£90k",
    }],
}


def test_build_message_escapes_html_and_has_all_parts():
    msg = td.build_message(dict(ROW), 1)
    assert "&lt;&amp; Co&gt;" in msg          # HTML in the title is escaped
    assert "<b>1. Example Org —" in msg
    assert "🎯 89/100" in msg
    assert "💰 £90k" in msg                    # compensation pulled from locations
    assert "London, hybrid" in msg
    assert 'href="https://example.com/jobs/1?a=1&amp;b=2"' in msg
    assert "A short vacancy summary." in msg


def test_build_message_fits_telegram_limit():
    row = dict(ROW, llm_summary="x" * 10000)
    msg = td.build_message(row, 1)
    assert len(msg) <= td.MESSAGE_MAX_CHARS
    assert "href=" in msg                      # the link survives truncation


def test_summary_fallback_to_description():
    row = dict(ROW, llm_summary=None, full_description="word " * 300)
    s = td.vacancy_summary(row)
    assert s.endswith("…")
    assert len(s) <= td.SUMMARY_FALLBACK_CHARS + 1


def test_vacancy_url_missing_locations():
    assert td.vacancy_url({"locations": None}) is None
    assert td.vacancy_url({"locations": [{"city": "X"}]}) is None


def test_keyboard_callback_roundtrip():
    kb = td.build_keyboard(ROW["id"])
    like_btn, pass_btn = kb["inline_keyboard"][0]
    assert td.parse_callback(like_btn["callback_data"]) == (ROW["id"], "liked")
    assert td.parse_callback(pass_btn["callback_data"]) == (ROW["id"], "passed")
    assert len(like_btn["callback_data"].encode()) <= 64  # Telegram limit


def test_keyboard_marks_chosen():
    kb = td.build_keyboard(ROW["id"], chosen="liked")
    assert kb["inline_keyboard"][0][0]["text"].startswith("✅")
    assert "✅" not in kb["inline_keyboard"][0][1]["text"]


def test_parse_callback_rejects_garbage():
    assert td.parse_callback(None) is None
    assert td.parse_callback("") is None
    assert td.parse_callback("x:y:z") is None
    assert td.parse_callback("v:abc:q") is None      # unknown action
    assert td.parse_callback("v::l") is None         # empty id
    assert td.parse_callback("v:abc:l:extra") is None

"""Tests for _sanitize_text, _parse_json."""
import pytest
from score_vacancies import _sanitize_text, _parse_json


# ---------------------------------------------------------------------------
# _sanitize_text
# ---------------------------------------------------------------------------

def test_ST01_crlf_normalized():
    result = _sanitize_text("line1\r\nline2")
    assert result == "line1\nline2"


def test_ST02_nbsp_replaced():
    result = _sanitize_text("hello\xa0world")
    assert result == "hello world"


def test_ST03_control_chars_removed():
    result = _sanitize_text("\x01 text")
    assert result == " text"


def test_ST04_normal_text_unchanged():
    text = "Hello World\nWith newline\tand tab"
    assert _sanitize_text(text) == text


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------

def test_PJ01_plain_valid_json():
    result = _parse_json('{"score": 75, "reasoning": "Good match"}')
    assert result["score"] == 75
    assert result["reasoning"] == "Good match"


def test_PJ02_fenced_json_block():
    text = '```json\n{"score": 60, "reasoning": "Partial"}\n```'
    result = _parse_json(text)
    assert result["score"] == 60


def test_PJ03_preamble_before_json():
    text = 'Here is my analysis:\n\n{"score": 80, "reasoning": "Strong fit"}'
    result = _parse_json(text)
    # Should parse the valid JSON object from the text
    # (plain json.loads fails, brace_match regex extracts it)
    assert "score" in result


def test_PJ04_invalid_returns_error_dict():
    result = _parse_json("this is not json at all")
    assert "error" in result
    assert "raw" in result

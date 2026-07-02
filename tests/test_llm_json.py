"""Tests for the shared LLM-response JSON parser (scripts/llm_json.py).

The two scorers delegate to parse_llm_json; this pins the shared behaviour and
the one difference between them — the brace-fallback pattern.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from llm_json import (  # noqa: E402
    FLAT_SCORE_OBJECT,
    OUTERMOST_BRACES,
    parse_llm_json,
)


def test_plain_json():
    assert parse_llm_json('{"score": 75, "reasoning": "ok"}') == {
        "score": 75,
        "reasoning": "ok",
    }


def test_fenced_json():
    assert parse_llm_json('```json\n{"score": 55}\n```') == {"score": 55}


def test_invalid_returns_error_marker():
    out = parse_llm_json("this is not json at all")
    assert out["error"] == "JSON parse failed"
    assert out["raw"] == "this is not json at all"


def test_flat_score_fallback_extracts_from_preamble():
    text = 'Here is my verdict: {"score": 42, "reasoning": "meh"} — done.'
    assert parse_llm_json(text, FLAT_SCORE_OBJECT) == {
        "score": 42,
        "reasoning": "meh",
    }


def test_outermost_braces_fallback_handles_nested_objects():
    text = 'Preamble {"alignment_score": 70, "about": {"sector": "climate"}} end'
    out = parse_llm_json(text, OUTERMOST_BRACES)
    assert out["alignment_score"] == 70
    assert out["about"] == {"sector": "climate"}


def test_flat_pattern_does_not_span_nested_braces():
    # The flat pattern intentionally cannot match an object with nested braces,
    # so a nested company-shaped payload falls through to the error marker when
    # the vacancy fallback is used (documents why the two callers differ).
    text = 'noise {"about": {"x": 1}, "score": 5} noise'
    out = parse_llm_json(text, FLAT_SCORE_OBJECT)
    assert out["error"] == "JSON parse failed"

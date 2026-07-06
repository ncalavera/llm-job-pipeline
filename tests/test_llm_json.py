"""Tests for the shared LLM-response JSON parser (scripts/llm_json.py).

The two scorers delegate to parse_llm_json; this pins the shared behaviour and
the one difference between them — the brace-fallback pattern.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from llm_json import (  # noqa: E402
    FLAT_SCORE_OBJECT,
    OUTERMOST_BRACES,
    parse_llm_json,
    read_result_files,
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


# ---------------------------------------------------------------------------
# read_result_files — BUG-5: one malformed result file must not kill the batch
# ---------------------------------------------------------------------------


def test_read_result_files_all_valid(tmp_path):
    f1 = tmp_path / "c1.json"
    f1.write_text('{"score": 70, "member_ids": ["a"]}', encoding="utf-8")
    f2 = tmp_path / "c2.json"
    f2.write_text('{"score": 40, "member_ids": ["b"]}', encoding="utf-8")

    items, bad = read_result_files([f1, f2])
    assert bad == []
    assert [i["member_ids"][0] for i in items] == ["a", "b"]


def test_read_result_files_skips_malformed_and_keeps_the_rest(tmp_path):
    good1 = tmp_path / "c27.json"
    good1.write_text('{"score": 70, "member_ids": ["a"]}', encoding="utf-8")
    bad = tmp_path / "c39.json"
    bad.write_text('{"score": 40, "member_ids": ["b"', encoding="utf-8")  # truncated
    good2 = tmp_path / "c42.json"
    good2.write_text('{"score": 90, "member_ids": ["c"]}', encoding="utf-8")

    items, bad_list = read_result_files([good1, bad, good2])
    assert [i["member_ids"][0] for i in items] == ["a", "c"]
    assert len(bad_list) == 1
    assert "c39.json" in bad_list[0]  # bad file named
    assert "Expecting" in bad_list[0] or "line" in bad_list[0].lower()  # parse error surfaced


def test_read_result_files_accepts_a_file_holding_an_array(tmp_path):
    f = tmp_path / "batch.json"
    f.write_text(
        json.dumps([{"score": 1, "member_ids": ["a"]}, {"score": 2, "member_ids": ["b"]}]),
        encoding="utf-8",
    )
    items, bad = read_result_files([f])
    assert bad == []
    assert len(items) == 2


def test_read_result_files_missing_file_is_reported_as_bad(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    items, bad = read_result_files([missing])
    assert items == []
    assert len(bad) == 1
    assert "does_not_exist.json" in bad[0]


def test_read_result_files_truncated_mid_multibyte_utf8_is_bad_not_crash(tmp_path):
    """A kill mid-write can cut the file inside a multi-byte UTF-8 character —
    read_text then raises UnicodeDecodeError BEFORE json even runs. That is the
    exact BUG-5 spend-limit-kill scenario, so it must be reported as a bad
    file, never raised out of the helper."""
    good = tmp_path / "c1.json"
    good.write_text('{"score": 70, "member_ids": ["a"]}', encoding="utf-8")
    cut = tmp_path / "c2.json"
    # "…é" is 0xC3 0xA9 in UTF-8; keep only the first byte — an invalid tail.
    cut.write_bytes('{"reasoning": "caf'.encode("utf-8") + b"\xc3")

    items, bad = read_result_files([good, cut])
    assert [i["member_ids"][0] for i in items] == ["a"]
    assert len(bad) == 1
    assert "c2.json" in bad[0]


def test_read_result_files_bare_scalar_is_reported_as_bad(tmp_path):
    """Valid JSON that is not an object/array (a stray "null", a number) is
    not a result — report it, don't pass junk downstream to --save."""
    good = tmp_path / "c1.json"
    good.write_text('{"score": 70, "member_ids": ["a"]}', encoding="utf-8")
    scalar = tmp_path / "c2.json"
    scalar.write_text("null", encoding="utf-8")

    items, bad = read_result_files([good, scalar])
    assert [i["member_ids"][0] for i in items] == ["a"]
    assert len(bad) == 1
    assert "c2.json" in bad[0]
    assert "not a JSON object or array" in bad[0]

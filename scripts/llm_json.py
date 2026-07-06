"""Shared parser for JSON embedded in an LLM text response.

Both scorers get free text back from the model and must recover the JSON object
inside it, tolerating code fences and preamble. The scorers were byte-identical
except for the last-resort brace fallback: vacancy scoring keys on a flat
``{"score": N}`` object, company scoring greedily matches the outermost braces
around nested objects. That one difference is now a parameter, so the parse
logic lives in exactly one place.
"""

import json
import re
from pathlib import Path

#: Vacancy scoring fallback — a flat object containing a numeric ``score`` key.
FLAT_SCORE_OBJECT = r'\{[^{}]*"score"\s*:\s*\d+[^{}]*\}'
#: Company scoring fallback — greedily match the outermost ``{ ... }`` (company
#: responses have nested objects).
OUTERMOST_BRACES = r"\{.*\}"


def parse_llm_json(text: str, brace_pattern: str = OUTERMOST_BRACES) -> dict:
    """Parse JSON from an LLM response, handling fences and preamble.

    Tries, in order: direct ``json.loads``; a ```` ```json ```` fenced block;
    then a brace-delimited fallback matched by ``brace_pattern``. Returns an
    ``{"error": ..., "raw": ...}`` marker if all three fail (never raises).
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace = re.search(brace_pattern, text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return {"error": "JSON parse failed", "raw": text[:500]}


def read_result_files(paths) -> tuple[list, list[str]]:
    """Read individual scoring-subagent result files for a ``--save`` batch,
    tolerating a malformed one (BUG-5: a spend-limit kill mid-write, or an
    unescaped quote in a string value, can leave one file's JSON invalid).

    The historical ``--save`` contract is one JSON blob (array or object) on
    stdin, assembled from every subagent's raw output — a single bad entry
    there corrupts the surrounding syntax and fails the ``json.load`` for the
    WHOLE batch (all-or-nothing). Reading files individually sidesteps that:
    each file is parsed on its own, so one bad file can never block the rest.

    Each file holds either a single JSON object (one vacancy/company result)
    or a JSON array of them. Returns ``(items, bad)``: ``items`` is every
    object that parsed cleanly, in file order; ``bad`` is a list of
    ``"<filename>: <error>"`` strings for files that failed to read or parse
    — printed by the caller so the bad ones are named and can be re-scored,
    never silently dropped. Never raises.
    """
    items: list = []
    bad: list[str] = []
    for path in paths:
        p = Path(path)
        try:
            # UnicodeDecodeError matters here: a kill mid multi-byte UTF-8
            # character makes read_text itself raise, before json even runs —
            # the exact truncation scenario this helper exists to survive.
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            bad.append(f"{p.name}: {exc}")
            continue
        if not isinstance(obj, (dict, list)):
            # A bare scalar (a stray "null", a number) is valid JSON but not a
            # result — report it as bad rather than passing junk downstream.
            bad.append(f"{p.name}: not a JSON object or array ({type(obj).__name__})")
            continue
        if isinstance(obj, list):
            items.extend(obj)
        else:
            items.append(obj)
    return items, bad

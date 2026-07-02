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

"""A missing ANTHROPIC_API_KEY must never be reported to the user as a fault.

The pipeline runs its LLM work through the agent it is already inside — headless
Claude sessions answering the driver's gates. A direct API key is an optional
shortcut for one stage, so its absence is the intended configuration, not a
degradation. See the "No direct-API key is a supported setup" section of
AGENTS.md.

This file exists because the rule was broken in practice: a night log line
reading "ANTHROPIC_API_KEY unset" was read as a defect, a warning was built
telling the user to add the key, and the user was asked three times to add a
key he had explicitly ruled out. Documentation alone did not prevent it; a
failing test does.
"""

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Files that render text a user actually reads: the phone, the report card,
# the console summary.
USER_FACING = ("i18n.py", "telegram_digest.py", "run_daily.py", "screen_candidates.py")

# "add/set/configure ... anthropic key" in any order, and the bare env var name
# sitting inside a quoted sentence rather than in code.
_NAG = re.compile(
    r"(add|set|configure|provide|missing|unset|absent)[^\n\"']{0,60}anthropic[^\n\"']{0,20}key"
    r"|anthropic[^\n\"']{0,20}key[^\n\"']{0,60}(is )?(missing|unset|not set|absent|required)",
    re.IGNORECASE,
)


def _strings_of(path: Path) -> list[str]:
    """Every quoted literal in the file, which is where user-facing text lives."""
    src = path.read_text(encoding="utf-8")
    return re.findall(r'"([^"\n]{4,})"|\'([^\'\n]{4,})\'', src) and [
        a or b for a, b in re.findall(r'"([^"\n]{4,})"|\'([^\'\n]{4,})\'', src)
    ]


@pytest.mark.parametrize("name", USER_FACING)
def test_no_user_facing_text_asks_for_an_anthropic_key(name):
    path = SCRIPTS / name
    if not path.exists():  # a file may be renamed; the rule is not about the name
        pytest.skip(f"{name} not present")
    offenders = [s for s in _strings_of(path) if _NAG.search(s)]
    assert not offenders, (
        f"{name} tells the user a missing ANTHROPIC_API_KEY is a problem: {offenders}. "
        "Running without that key is the intended setup — screening falls to the "
        "subagent path. See AGENTS.md, 'No direct-API key is a supported setup'."
    )


def test_the_key_never_reaches_a_claude_child():
    """The scoring sessions run on the subscription login. A key in the child's
    environment would be preferred over it and move the spend to per-token
    billing — silently, and only visible on a bill."""
    import sys

    sys.path.insert(0, str(SCRIPTS))
    import nightly_run

    assert "ANTHROPIC_API_KEY" not in nightly_run._CHILD_ENV_ALLOWLIST, (
        "ANTHROPIC_API_KEY is back in the Claude child's environment allowlist. "
        "A session that sees it bills per token instead of using the subscription."
    )
    # Still masked in logs and alerts if it exists in the parent for other tools.
    assert "ANTHROPIC_API_KEY" in nightly_run._SECRET_ENV_KEYS

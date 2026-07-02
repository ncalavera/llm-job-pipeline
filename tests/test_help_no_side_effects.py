"""Regression: ``--help`` prints usage WITHOUT side effects.

``python3 scripts/<entrypoint>.py --help`` must construct its argparse parser
before the heavy project imports, so it never:
  - connects to a database  ("  SQLite: connected", "Backend:")
  - loads the user profile  (the missing-profile EXAMPLE warning)

These side effects on ``--help`` made the CLI slow, noisy, and dependent on a
configured DB just to read usage. Each entrypoint is run as a real subprocess
with a CLEAN environment (no DB configured) and must exit 0, print "usage",
and emit none of the side-effect markers on stdout OR stderr.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

ENTRYPOINTS = [
    "fetch_vacancies.py",
    "score_vacancies.py",
    "vac.py",
    "filter_vacancies.py",
]

# Side-effect markers that must NOT appear in --help output. Sourced from the
# real strings: db_backend.py ("  SQLite: connected (...)", "Backend: ...") and
# prompts.py (the EXAMPLE-profile fallback warning).
DB_MARKERS = ["SQLite: connected", "Backend:"]
PROFILE_WARNING = "using the EXAMPLE profile"


def _clean_env() -> dict:
    """Env with no DB configured, so a leaked DB connect would be observable."""
    env = dict(os.environ)
    for var in ("SUPABASE_DB_URL", "SUPABASE_DIRECT_URL", "JOBSEARCH_DB_PATH"):
        env.pop(var, None)
    return env


@pytest.mark.parametrize("entry", ENTRYPOINTS)
def test_help_has_no_side_effects(entry):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / entry), "--help"],
        capture_output=True,
        text=True,
        env=_clean_env(),
        timeout=60,
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"{entry} --help exited {proc.returncode}\n{combined}"
    assert "usage" in combined.lower(), f"{entry} --help printed no usage:\n{combined}"

    for marker in DB_MARKERS:
        assert marker not in combined, (
            f"{entry} --help connected to the DB (found {marker!r}):\n{combined}"
        )
    assert PROFILE_WARNING not in combined, (
        f"{entry} --help loaded the profile (EXAMPLE warning):\n{combined}"
    )

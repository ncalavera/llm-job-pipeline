"""Trial T4 — "honest demo".

Reproduces the first user-test failure where the SQLite-vs-Supabase split read as
infrastructure trivia with no guidance. The persona tries the tool with no
``.env`` at all: simple mode must work end to end, name itself honestly, and —
when they later move to Supabase — fail with the message the docs promised.

Most of this trial is a manual protocol (actually standing up Supabase). The
cheap slices are automated here: simple mode works with no ``.env``, and the
messages the code emits are the ones INSTALL-EASY.md documents. The loader
mechanics themselves are unit-tested in ``test_env_loader.py`` and
``test_simple_mode_no_psycopg2.py``; this trial ties those messages to the docs.
"""

from __future__ import annotations

import io
import os

import trial_harness as h

INSTALL_EASY = os.path.join(h.REPO_ROOT, "INSTALL-EASY.md")
DB_BACKEND_SRC = os.path.join(h.SCRIPTS, "db_backend.py")

# The exact strings a user reads. Both must appear in the code that emits them AND
# in the doc that promises them — a drift on either side is a T4 failure.
SQLITE_BANNER = "Backend: local SQLite"
PSYCOPG2_MESSAGE = "psycopg2 is not installed"


def test_simple_mode_works_with_no_env(monkeypatch, tmp_path):
    """No .env, no Supabase, empty local DB — the demo actually runs and stores data."""
    dal = h.use_persona(monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db")

    import db_backend

    assert db_backend.IS_SQLITE, "with no SUPABASE_DB_URL the demo must stay on SQLite"

    saved = h.seed_roles(dal, "Demo Co", [("Platform Engineer", "Run the platform. " * 12)])
    assert saved == 1
    loaded = dal.load_vacancies()
    assert any(v["org"] == "Demo Co" for v in loaded.values()), "the demo round-trips a vacancy"


def test_backend_banner_is_the_documented_sqlite_message(monkeypatch, tmp_path):
    """The banner names SQLite honestly and matches what INSTALL-EASY.md shows."""
    h.use_persona(
        monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db", migrate=False
    )

    import db_backend

    buf = io.StringIO()
    db_backend.print_backend_banner(buf)
    banner = buf.getvalue()

    assert SQLITE_BANNER in banner
    assert "Postgres" not in banner and "Supabase" not in banner, "no false parity promise"
    assert "WARNING" not in banner, "a plain simple-mode run has nothing to warn about"

    doc = _read(INSTALL_EASY)
    assert SQLITE_BANNER in doc, "the demo banner the user sees must be documented verbatim"


def test_supabase_transition_failure_message_is_documented():
    """The move-to-Supabase failure message the docs promise is the one the code emits."""
    doc = _read(INSTALL_EASY)
    src = _read(DB_BACKEND_SRC)

    assert PSYCOPG2_MESSAGE in src, "the code must emit the documented psycopg2 message"
    assert PSYCOPG2_MESSAGE in doc, "INSTALL-EASY.md must promise the message the code emits"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()

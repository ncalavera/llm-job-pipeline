"""The linked-worktree profile trap — resolution fallback + prod bake guard.

``config/user_profile.md`` is gitignored (personal data), so a ``git worktree``
never carries it. A pipeline run launched from a worktree would fall through to
the bundled EXAMPLE profile and bake DEFAULT settings (language, thresholds…)
into the SINGLE shared ``dashboard_snapshot`` row in prod Supabase — silently
overwriting the owner's real configuration. Two layers close it:

  1. resolution recovers the REAL profile from the MAIN checkout's ``config/``
     when a linked worktree lacks its own copy (``prompts``);
  2. a fail-safe: on the Postgres/Supabase backend, if NO real profile resolves,
     ``generate_dashboard`` REFUSES to write rather than bake the example into
     shared state. SQLite/local stays permissive (fresh installs, tests).

Offline, invented data, real throwaway git repos under tmp_path — never the
maintainer's files or a live DB.
"""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompts  # noqa: E402


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    prompts.clear_profile_cache()
    yield
    prompts.clear_profile_cache()


# ---------------------------------------------------------------------------
# 1. Worktree-fallback resolution — the real profile is recovered from main
# ---------------------------------------------------------------------------


def _make_main_with_worktree(tmp_path: Path, profile_body: str) -> tuple[Path, Path]:
    """Build a main checkout whose gitignored profile lives only in its working
    tree, plus a linked worktree that (correctly) lacks that file. Returns
    ``(main, worktree)``."""
    main = tmp_path / "main"
    (main / "config").mkdir(parents=True)
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "Test")
    # user_profile.md is gitignored — so it never enters the worktree checkout.
    (main / ".gitignore").write_text("config/user_profile.md\n", encoding="utf-8")
    _git(main, "add", ".gitignore")
    _git(main, "commit", "-q", "-m", "init")
    (main / "config" / "user_profile.md").write_text(profile_body, encoding="utf-8")

    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree))
    return main, worktree


def _point_prompts_at(monkeypatch, root: Path) -> None:
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    monkeypatch.setattr(prompts, "_REPO_ROOT", root)
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", root / "config" / "user_profile.md")
    monkeypatch.setattr(
        prompts, "EXAMPLE_PROFILE_PATH", root / "config" / "user_profile.example.md"
    )
    prompts.clear_profile_cache()


def test_worktree_without_profile_reads_main_checkout(tmp_path, monkeypatch, capsys):
    body = "## USER_PROFILE\n\nReal person.\n\n## OUTPUT_LANGUAGE\n\nRussian\n"
    main, worktree = _make_main_with_worktree(tmp_path, body)
    assert not (worktree / "config" / "user_profile.md").exists()

    _point_prompts_at(monkeypatch, worktree)

    path, warn_example, from_worktree = prompts._resolve_profile_path()
    assert path.resolve() == (main / "config" / "user_profile.md").resolve()
    assert warn_example is False
    assert from_worktree is True

    # The real profile's content is what gets parsed — not the example.
    sections = prompts._load_user_profile()
    assert sections.get("OUTPUT_LANGUAGE") == "Russian"
    assert prompts.has_real_profile() is True

    # One clear line explains the recovery, once.
    err = capsys.readouterr().err
    assert "git worktree" in err
    assert "main checkout" in err


def test_main_checkout_is_not_treated_as_a_worktree(tmp_path, monkeypatch):
    """A plain checkout (git-dir == common-dir) must NOT trigger the fallback."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    _git(repo, "init", "-q")
    monkeypatch.setattr(prompts, "_REPO_ROOT", repo)
    prompts.clear_profile_cache()
    assert prompts._worktree_main_profile() is None


def test_worktree_fallback_absent_when_main_has_no_profile(tmp_path, monkeypatch):
    """Worktree + a main checkout that itself lacks the profile → no recovery,
    so resolution degrades to the EXAMPLE (has_real_profile False)."""
    main = tmp_path / "main"
    (main / "config").mkdir(parents=True)
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "Test")
    (main / ".gitignore").write_text("config/user_profile.md\n", encoding="utf-8")
    _git(main, "add", ".gitignore")
    _git(main, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree))

    (worktree / "config").mkdir(parents=True, exist_ok=True)
    (worktree / "config" / "user_profile.example.md").write_text(
        "## USER_PROFILE\n\nExample.\n", encoding="utf-8"
    )
    _point_prompts_at(monkeypatch, worktree)

    assert prompts._worktree_main_profile() is None
    path, warn_example, from_worktree = prompts._resolve_profile_path()
    assert warn_example is True  # degraded to the bundled example
    assert from_worktree is False
    assert prompts.has_real_profile() is False


# ---------------------------------------------------------------------------
# 2 & 3. The prod bake guard — Postgres refuses, SQLite stays permissive
# ---------------------------------------------------------------------------


def _load_report(monkeypatch):
    """Reload report (and its db_backend dependency) with a clean SQLite chain."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    for mod in ("database_supabase", "config", "db_conn", "db_backend", "report"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    import report

    return report, db_backend


def _current_prompts():
    """The ``prompts`` module the guard's ``import prompts`` will resolve.

    Bind it from ``sys.modules`` at call time: other tests reload the module via
    ``sys.modules`` surgery, so the file-level ``prompts`` import can go stale and
    monkeypatching it would miss the object the guard actually sees.
    """
    import prompts as _p

    return _p


def test_postgres_refuses_to_bake_example_profile(monkeypatch):
    """Postgres backend + no real profile → guard raises, naming the file/trap."""
    report, db_backend = _load_report(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: False)

    with pytest.raises(RuntimeError) as exc:
        report._guard_shared_snapshot_profile()
    msg = str(exc.value)
    assert "user_profile.md" in msg
    assert "worktree" in msg.lower()


def test_generate_dashboard_refuses_before_any_db_read(monkeypatch):
    """The guard fires at the TOP of generate_dashboard — nothing is read or
    written when the profile is missing on Postgres."""
    report, db_backend = _load_report(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("generate_dashboard read data before the profile guard")

    monkeypatch.setattr(report, "prepare_report_data", _boom)
    with pytest.raises(RuntimeError):
        report.generate_dashboard()


def test_postgres_allows_write_with_real_profile(monkeypatch):
    """A real profile (default file, USER_PROFILE_PATH, or worktree fallback) →
    the guard is a no-op even on Postgres."""
    report, db_backend = _load_report(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: True)
    report._guard_shared_snapshot_profile()  # must not raise


def test_sqlite_stays_permissive_with_missing_profile(monkeypatch, tmp_path):
    """SQLite/local keeps the permissive fallback: a missing profile never blocks
    the local data.js bake (fresh installs, tests)."""
    report, db_backend = _load_report(monkeypatch)
    assert db_backend.IS_SQLITE, "this test must run on the SQLite backend"
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: False)

    report._guard_shared_snapshot_profile()  # no raise on SQLite

    out_dir = tmp_path / "public_out"
    out_dir.mkdir()
    monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)
    report._persist_dashboard({"groups": [{"id": "x"}]})
    assert [p.name for p in out_dir.iterdir()] == ["data.js"]

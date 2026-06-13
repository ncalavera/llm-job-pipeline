"""Profile-safety: falling back to the EXAMPLE profile must warn loudly.

Without config/user_profile.md the loader silently used the bundled EXAMPLE
(a fictional person), so a user could score an entire run against fiction and
never know. The loader must print a prominent stderr warning on that fallback —
without hard-crashing, and without warning when a real profile is used.
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompts  # noqa: E402


def test_example_fallback_warns(monkeypatch, tmp_path, capsys):
    """No env override + no real profile + example present → stderr warning."""
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    missing = tmp_path / "user_profile.md"           # does NOT exist
    example = tmp_path / "user_profile.example.md"
    example.write_text("## USER_PROFILE\n\nExample person.\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", missing)
    monkeypatch.setattr(prompts, "EXAMPLE_PROFILE_PATH", example)

    sections = prompts._load_user_profile()  # must NOT raise
    assert "USER_PROFILE" in sections

    err = capsys.readouterr().err
    assert "EXAMPLE profile" in err
    assert "MEANINGLESS" in err


def test_real_profile_does_not_warn(monkeypatch, tmp_path, capsys):
    """A real config/user_profile.md → no warning."""
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    real = tmp_path / "user_profile.md"
    real.write_text("## USER_PROFILE\n\nReal person.\n", encoding="utf-8")
    example = tmp_path / "user_profile.example.md"
    example.write_text("## USER_PROFILE\n\nExample.\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", real)
    monkeypatch.setattr(prompts, "EXAMPLE_PROFILE_PATH", example)

    prompts._load_user_profile()
    err = capsys.readouterr().err
    assert "EXAMPLE profile" not in err


def test_explicit_env_path_does_not_warn(monkeypatch, tmp_path, capsys):
    """An explicit USER_PROFILE_PATH (how tests pass profiles) never warns."""
    profile = tmp_path / "p.md"
    profile.write_text("## USER_PROFILE\n\nExplicit.\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))

    prompts._load_user_profile()
    err = capsys.readouterr().err
    assert "EXAMPLE profile" not in err


def test_no_profile_at_all_raises(monkeypatch, tmp_path):
    """Neither real nor example present → explicit FileNotFoundError, not a
    silent empty profile."""
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", tmp_path / "nope.md")
    monkeypatch.setattr(prompts, "EXAMPLE_PROFILE_PATH", tmp_path / "nope.example.md")
    with pytest.raises(FileNotFoundError):
        prompts._load_user_profile()

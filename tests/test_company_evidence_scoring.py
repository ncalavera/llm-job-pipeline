"""score_companies reads company_evidence, and shouts when it is missing.

* _load_company_evidence_map runs on SQLite (the query is backend-agnostic — no
  ``::text`` cast that only Postgres understands).
* cmd_local builds the WANT payload FROM company_evidence when present.
* cmd_local emits a LOUD warning for any scored company with NO evidence
  (falling back to the legacy scrape cache), instead of degrading silently.

Fully offline on the SQLite backend.
"""

import importlib
import io
import json
import sys
import types
from contextlib import redirect_stderr

import pytest


@pytest.fixture()
def sc(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in (
        "score_companies",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase as dal
    import score_companies

    importlib.reload(score_companies)

    ns = types.SimpleNamespace(dal=dal, mod=score_companies)
    yield ns
    dal.close_conn()


def _add_candidate(dal, name, website):
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO company (canonical_name, status, website) VALUES (%s, 'candidate', %s)",
        (name, website),
    )
    dal.get_conn().commit()
    cur.execute("SELECT id FROM company WHERE canonical_name = %s", (name,))
    cid = cur.fetchone()[0]
    cur.close()
    return cid


def _add_evidence(dal, cid, source, content):
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO company_evidence (company_id, source, url, content) VALUES (%s, %s, %s, %s)",
        (cid, source, "https://example.org/", content),
    )
    dal.get_conn().commit()
    cur.close()


def _run_local(sc, company=None):
    """Run cmd_local, capturing stdout (JSON payloads) and stderr (diagnostics)."""
    err = io.StringIO()
    out = io.StringIO()
    real = sys.stdout
    with redirect_stderr(err):
        sys.stdout = out
        try:
            sc.mod.cmd_local(types.SimpleNamespace(company=company, limit=None, dry_run=False))
        finally:
            sys.stdout = real
    payloads = json.loads(out.getvalue() or "[]")
    return payloads, err.getvalue()


def test_load_company_evidence_map_works_on_sqlite(sc):
    cid = _add_candidate(sc.dal, "Nova Harbor", "https://novaharbor.org")
    _add_evidence(sc.dal, cid, "website", "Nova Harbor is a climate-data nonprofit. " * 5)

    emap = sc.mod._load_company_evidence_map([cid])
    assert str(cid) in emap
    assert emap[str(cid)][0]["source"] == "website"


def test_payload_built_from_evidence_no_warning(sc):
    cid = _add_candidate(sc.dal, "Nova Harbor", "https://novaharbor.org")
    _add_evidence(sc.dal, cid, "website", "Nova Harbor is a climate-data nonprofit. " * 5)

    payloads, stderr = _run_local(sc, company="Nova Harbor")

    assert len(payloads) == 1
    assert "Nova Harbor is a climate-data nonprofit" in payloads[0]["user_msg"]
    assert "companies have NO" not in stderr  # the degradation warning must be absent
    assert "strategy.md" not in stderr
    assert "{strategy_context}" not in payloads[0]["system_prompt"]
    import prompts

    profile = prompts._load_user_profile()
    for key in ("USER_PROFILE", "TARGET_ROLES", "EXCLUDE_PATTERNS"):
        assert profile[key] in payloads[0]["system_prompt"]


def test_missing_evidence_triggers_loud_warning(sc, monkeypatch):
    """A candidate with a scrape-cache entry but NO company_evidence is scored via
    the legacy fallback — and that MUST print the loud warning."""
    _add_candidate(sc.dal, "Drift Labs", "https://driftlabs.example")
    monkeypatch.setattr(
        sc.mod,
        "_load_scrape_cache",
        lambda: {"Drift Labs": ("https://driftlabs.example", "Drift Labs builds tools. " * 20)},
    )

    payloads, stderr = _run_local(sc, company="Drift Labs")

    assert len(payloads) == 1  # still scored (fallback), not dropped
    assert "companies have NO" in stderr
    assert "Drift Labs" in stderr
    assert "collect_company_evidence" in stderr  # the fix instruction is named


# ---------------------------------------------------------------------------
# _assemble_evidence_content — respects the configured char cap
# (settings.scoring()["company_evidence_char_cap"], read once into the module
# constant _EVIDENCE_TOTAL_CAP). Pure-function tests, no DB rows needed.
# ---------------------------------------------------------------------------


def test_assemble_evidence_content_under_cap_is_untouched(sc, monkeypatch):
    monkeypatch.setattr(sc.mod, "_EVIDENCE_TOTAL_CAP", 1000)
    rows = [{"source": "website", "url": "https://example.org", "content": "Short content."}]

    result = sc.mod._assemble_evidence_content(rows)

    assert result == "### SOURCE: website (https://example.org)\nShort content."
    assert "[trimmed]" not in result


def test_assemble_evidence_content_over_cap_is_trimmed(sc, monkeypatch):
    monkeypatch.setattr(sc.mod, "_EVIDENCE_TOTAL_CAP", 200)
    rows = [
        {"source": "website", "url": "https://example.org", "content": "A" * 300},
        {"source": "careers", "url": "https://example.org/careers", "content": "B" * 300},
    ]
    combined_uncapped = "".join(r["content"] for r in rows)

    result = sc.mod._assemble_evidence_content(rows)

    # Trimming actually shrank the payload well below the raw combined size.
    assert len(result) < len(combined_uncapped)
    # Both source labels survive -- trimming cuts content, not structure.
    assert "### SOURCE: website (https://example.org)" in result
    assert "### SOURCE: careers (https://example.org/careers)" in result
    # Each source keeps its HEAD (where the material anchors sit), not a
    # random slice or the tail.
    assert "AAA" in result and "A" * 300 not in result
    assert "BBB" in result and "B" * 300 not in result
    assert result.count("[trimmed]") == 2


def test_assemble_evidence_content_trims_proportionally_by_share(sc, monkeypatch):
    """A source with 3x the content of another keeps roughly 3x as much after
    trimming -- the budget split is proportional, not equal."""
    monkeypatch.setattr(sc.mod, "_EVIDENCE_TOTAL_CAP", 400)
    rows = [
        {"source": "website", "url": "https://example.org", "content": "A" * 900},
        {"source": "careers", "url": "https://example.org/careers", "content": "B" * 300},
    ]

    result = sc.mod._assemble_evidence_content(rows)

    kept_a = result.count("A")
    kept_b = result.count("B")
    assert kept_a > kept_b
    # Roughly a 3:1 split (allow slack for the shared label/separator budget).
    assert 2.0 < kept_a / kept_b < 4.0


def test_assemble_evidence_content_empty_rows_is_empty_string(sc):
    assert sc.mod._assemble_evidence_content([]) == ""

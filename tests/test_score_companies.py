"""Tests for the company scoring flow (scripts/score_companies.py).

Mirrors test_score_vacancies.py: the external boundary (the LLM) is replaced by
a plain Python dict standing in for a parsed model response, and we assert the
real scoring→persistence logic writes the company's mission_fit / alignment /
tier fields. Fully offline on the local SQLite backend.

What's exercised:
  * _parse_json / _extract_enrichment — the pure parse + normalize step that
    turns a raw LLM response into the enrichment dict the DAL persists.
  * save_company_enrichment — the persistence the --api backend calls, asserted
    to round-trip alignment_score, mission_fit and tier.
  * company_evidence — cmd_local builds the WANT payload from company_evidence
    and shouts when a scored company has none.
  * the profile-driven rubric — the desirability rubric is rendered from the
    USER PROFILE, not baked into the template, and carries no foreign anchors
    or worldview tokens (reuses the WORLDVIEW_TOKEN denylist defined in
    test_no_hardcoded_data.py, the public-repo guard — that cross-file import
    is preserved exactly, and that file is not touched here).
  * the custom boost key — a user-configured mission_fit boost key reaches
    scoring/tiering under both its configured and legacy name.

Note on cmd_save: score_companies.cmd_save now imports ``Json`` from
``db_backend`` (the backend-aware shim), so its parameter binding works on both
the SQLite and Postgres backends. See test_cmd_save_persists_on_sqlite.

Absorbed, in order: tests/test_company_evidence_scoring.py,
tests/test_company_scoring_profile_driven.py, tests/test_custom_boost_key.py.
"""

import importlib
import io
import json
import sys
import types
from contextlib import redirect_stderr
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompts  # noqa: E402
import score_companies  # noqa: E402
from database_supabase import calculate_company_tier  # noqa: E402
from test_no_hardcoded_data import WORLDVIEW_TOKEN  # noqa: E402


@pytest.fixture()
def sc(tmp_path, monkeypatch):
    """SQLite-backed DAL + a fresh score_companies module bound to a temp DB."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

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

    ns = type("ScEnv", (), {})()
    ns.dal = dal
    ns.mod = score_companies
    yield ns
    dal.close_conn()


# ---------------------------------------------------------------------------
# _parse_json — LLM response extraction (the external-boundary parse step)
# ---------------------------------------------------------------------------


def test_parse_json_plain(sc):
    out = sc.mod._parse_json('{"alignment_score": 70}')
    assert out["alignment_score"] == 70


def test_parse_json_fenced(sc):
    out = sc.mod._parse_json('```json\n{"alignment_score": 55}\n```')
    assert out["alignment_score"] == 55


def test_parse_json_nested_with_preamble(sc):
    text = 'Analysis:\n{"about": {"description": "x"}, "mission_fit": {"alignment_score": 80}}'
    out = sc.mod._parse_json(text)
    assert out["mission_fit"]["alignment_score"] == 80


def test_parse_json_invalid_returns_error(sc):
    out = sc.mod._parse_json("not json")
    assert "error" in out and "raw" in out


# ---------------------------------------------------------------------------
# _extract_enrichment — normalize a parsed LLM result into an enrichment dict
# ---------------------------------------------------------------------------


def test_extract_enrichment_from_mission_fit(sc):
    result = {
        "about": {"description": "A robotics nonprofit", "sector": "Robotics"},
        "mission_fit": {
            "alignment_score": 72,
            "alignment_label": "strong",
            "strengths": ["mission overlap"],
            "risks": ["early stage"],
        },
    }
    enr = sc.mod._extract_enrichment(result)
    assert enr is not None
    assert enr["alignment_score"] == 72
    assert enr["mission_fit"]["alignment_label"] == "strong"
    assert enr["mission_fit"]["strengths"] == ["mission overlap"]
    # about carries the enrichment provenance stamp.
    assert enr["about"]["about_source"] == "llm_from_markdown"


def test_extract_enrichment_top_level_alignment_fallback(sc):
    """alignment_score at the top level (not nested) is still picked up."""
    result = {"alignment_score": 40, "mission_fit": {}}
    enr = sc.mod._extract_enrichment(result)
    assert enr is not None
    assert enr["alignment_score"] == 40


def test_extract_enrichment_missing_alignment_is_none(sc):
    assert sc.mod._extract_enrichment({"about": {"description": "x"}}) is None


def test_extract_enrichment_out_of_range_is_none(sc):
    assert sc.mod._extract_enrichment({"alignment_score": 250}) is None
    assert sc.mod._extract_enrichment({"alignment_score": -5}) is None


# ---------------------------------------------------------------------------
# Scoring → persistence: parse a (fake) LLM result and persist via the DAL,
# exactly as the --api backend does. Asserts mission_fit / alignment land.
# ---------------------------------------------------------------------------


def test_score_result_persists_to_company_row(sc):
    db = sc.dal
    db.ensure_company("Acme Robotics", status="candidate")
    db.get_conn().commit()

    # Stand-in for a parsed model response (no network, no LLM).
    fake_llm_result = sc.mod._parse_json(
        '{"about": {"description": "Robotics for good", "sector": "Robotics"},'
        ' "mission_fit": {"alignment_score": 78, "alignment_label": "strong",'
        ' "strengths": ["impact"], "approach": "apply directly"}}'
    )
    enr = sc.mod._extract_enrichment(fake_llm_result)
    assert enr is not None

    # Persist through the same boundary cmd_api uses.
    db.save_company_enrichment(
        "Acme Robotics",
        about=enr["about"],
        mission_fit=enr["mission_fit"],
        alignment_score=enr["alignment_score"],
    )
    db.get_conn().commit()

    saved = db.load_company_enrichment("Acme Robotics")
    assert saved["alignment_score"] == 78
    assert saved["mission_fit"]["alignment_label"] == "strong"
    assert saved["mission_fit"]["strengths"] == ["impact"]
    assert saved["about"]["description"] == "Robotics for good"


def test_calculate_company_tier_assigns_letter(sc):
    """A high alignment score maps to a tier letter via the shared helper."""
    tier, composite = sc.dal.calculate_company_tier(95)
    assert tier in {"S", "A", "B", "C"}
    assert composite == 95.0
    # None alignment → no tier.
    assert sc.dal.calculate_company_tier(None) == (None, None)


# ---------------------------------------------------------------------------
# cmd_save persists on the SQLite backend via db_backend's Json shim.
# ---------------------------------------------------------------------------


def test_cmd_save_persists_on_sqlite(sc):
    """score_companies.cmd_save binds JSON columns with db_backend's Json shim,
    which the SQLite cursor serializes, so the full stdin→DB save path persists
    on the SQLite backend (previously a Postgres-only path that silently no-op'd
    because it used the real psycopg2 Json)."""
    import io
    import json
    import types

    db = sc.dal
    cid = db.ensure_company("Acme Robotics", status="candidate")
    db.get_conn().commit()

    payload = [
        {
            "payload_kind": "company",
            "id": str(cid),
            "canonical_name": "Acme Robotics",
            "enrichment": {
                "about": {"description": "x", "sector": "Robotics"},
                "mission_fit": {"alignment_score": 70, "alignment_label": "ok"},
                "alignment_score": 70,
            },
        }
    ]
    monkeypatch_stdin = io.StringIO(json.dumps(payload))
    old_stdin = sys.stdin
    sys.stdin = monkeypatch_stdin
    try:
        sc.mod.cmd_save(types.SimpleNamespace(no_auto_review=True))
    finally:
        sys.stdin = old_stdin

    saved = db.load_company_enrichment("Acme Robotics")
    assert saved["alignment_score"] == 70
    assert saved["mission_fit"]["alignment_label"] == "ok"
    assert saved["about"]["description"] == "x"
    assert saved["about"]["about_source"] == "llm_subagent"


# ---------------------------------------------------------------------------
# cmd_save — BUG-5: one malformed result file must not kill the whole batch
# ---------------------------------------------------------------------------


def test_cmd_save_files_mode_skips_malformed_and_saves_rest(sc, tmp_path, capsys):
    """--files reads each company result file independently: a malformed one
    (truncated by a spend-limit kill) is named and skipped, the rest still
    save — matches the observed BUG-5 failure (c27/c39/c42 in one run)."""
    import json
    import types

    db = sc.dal
    cid = db.ensure_company("Acme Robotics", status="candidate")
    db.get_conn().commit()

    good = tmp_path / "c27.json"
    good.write_text(
        json.dumps(
            {
                "payload_kind": "company",
                "id": str(cid),
                "canonical_name": "Acme Robotics",
                "enrichment": {
                    "about": {"description": "x", "sector": "Robotics"},
                    "mission_fit": {"alignment_score": 70, "alignment_label": "ok"},
                    "alignment_score": 70,
                },
            }
        ),
        encoding="utf-8",
    )
    bad = tmp_path / "c39.json"
    bad.write_text('{"payload_kind": "company", "canonical_name": "Bad Co",', encoding="utf-8")

    sc.mod.cmd_save(types.SimpleNamespace(no_auto_review=True, files=[str(good), str(bad)]))

    saved = db.load_company_enrichment("Acme Robotics")
    assert saved["alignment_score"] == 70  # the good file saved despite the bad one

    out = capsys.readouterr()
    assert "c39.json" in out.err
    assert "Skipped 1 malformed file" in out.out


# ===========================================================================
# --- from test_company_evidence_scoring.py ---
#
# score_companies reads company_evidence, and shouts when it is missing.
#
# * _load_company_evidence_map runs on SQLite (the query is backend-agnostic —
#   no ``::text`` cast that only Postgres understands).
# * cmd_local builds the WANT payload FROM company_evidence when present.
# * cmd_local emits a LOUD warning for any scored company with NO evidence
#   (falling back to the legacy scrape cache), instead of degrading silently.
#
# Fully offline on the SQLite backend. Reuses the ``sc`` fixture above (the
# source file's own ``sc`` fixture was byte-for-byte the same setup — same
# module list, same yield shape — so it is dropped here, not duplicated).
# ===========================================================================


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


# ===========================================================================
# --- from test_company_scoring_profile_driven.py ---
#
# The company-scoring rubric is built from the USER PROFILE, not baked in.
#
# The company scorer must take its desirability rubric — which domains are
# TOP / MID / LOW, the reference organisations, the salary benchmark — from
# the user profile, exactly the way vacancy scoring does. This suite proves
# the shipped template is owner-agnostic: TWO different synthetic profiles (a
# software engineer and a nurse) render MEANINGFULLY DIFFERENT rubrics from
# the SAME template with zero template edits, and no foreign sector/org/money
# anchor survives.
#
# Fully offline: it renders the prompt from fixture profiles via the same
# `prompts._render` path production uses; it never calls an LLM.
#
# WORLDVIEW_TOKEN is imported from tests/test_no_hardcoded_data.py (the
# public-repo static guard) — that cross-file import is preserved exactly, as
# required; test_no_hardcoded_data.py itself is untouched.
# ===========================================================================

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENGINEER = FIXTURES / "profile_engineer.md"
MEDIC = FIXTURES / "profile_medic.md"


def _render_company_prompt(monkeypatch, profile_path: Path) -> str:
    """Render scripts/prompts/company-scoring.md against a given profile using
    the production template + substitution path."""
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile_path))
    prompts.clear_profile_cache()
    sections = prompts._load_user_profile()
    template = prompts._load_template("company-scoring.md")
    return prompts._render(template, sections)


# ---------------------------------------------------------------------------
# The rubric follows the profile
# ---------------------------------------------------------------------------


def test_engineer_profile_injects_its_own_domains(monkeypatch):
    rendered = _render_company_prompt(monkeypatch, ENGINEER)
    # The engineer's stated domains reach the rubric.
    assert "developer tools" in rendered
    assert "cloud infrastructure" in rendered
    # Their target roles and anti-list are injected too.
    assert "Staff Engineer" in rendered
    assert "Adtech" in rendered


def test_medic_profile_injects_its_own_domains(monkeypatch):
    rendered = _render_company_prompt(monkeypatch, MEDIC)
    assert "public health delivery" in rendered
    assert "hospital" in rendered.lower()
    assert "Clinical Nurse Specialist" in rendered
    assert "Tobacco" in rendered


def test_two_profiles_render_different_rubrics(monkeypatch):
    """Same template, different profiles → different rubric text. This is the
    core promise: no template edit is needed to re-target the scorer."""
    eng = _render_company_prompt(monkeypatch, ENGINEER)
    med = _render_company_prompt(monkeypatch, MEDIC)
    assert eng != med
    # Each rubric carries the OTHER's domains nowhere.
    assert "developer tools" in eng and "developer tools" not in med
    assert "Clinical Nurse Specialist" in med and "Clinical Nurse Specialist" not in eng


# ---------------------------------------------------------------------------
# The domain tiers are generated from the profile, not a fixed list
# ---------------------------------------------------------------------------


def test_domain_tiers_are_profile_generated(monkeypatch):
    rendered = _render_company_prompt(monkeypatch, ENGINEER)
    # The instruction tells the model to build the tiers from the candidate's
    # own profile rather than a fixed sector list.
    assert "BUILD THE TIERS FROM THE CANDIDATE'S OWN PROFILE" in rendered
    # TOP tier is defined by the candidate's own "want to work in" / target
    # sectors, and OFF tier by their EXCLUDE PATTERNS — not by hardcoded sectors.
    assert "TARGET ROLES" in rendered
    assert "EXCLUDE PATTERNS" in rendered


def test_all_profile_placeholders_substituted(monkeypatch):
    """No raw {{...}} placeholder survives for the profile-sourced sections."""
    for profile in (ENGINEER, MEDIC):
        rendered = _render_company_prompt(monkeypatch, profile)
        for ph in ("{{USER_PROFILE}}", "{{TARGET_ROLES}}", "{{EXCLUDE_PATTERNS}}"):
            assert ph not in rendered, f"{ph} left unsubstituted for {profile.name}"


# ---------------------------------------------------------------------------
# No foreign anchors survive in the rendered rubric
# ---------------------------------------------------------------------------

# Owner-specific reference organisations, network-prestige tokens and fixed
# sector labels that must never appear in an owner-agnostic template.
_FORBIDDEN_ANCHORS = [
    "givewell",
    "gavi",
    "malengo",
    "coefficient",
    "google.org",
    "global fund",
    "open phil",
    "rockefeller",
    "gwwc",
    "80k",
    "longtermism",
    "€7k",
]


def test_no_foreign_anchor_survives_for_a_neutral_profile(monkeypatch):
    """Neither synthetic profile mentions impact-sector orgs, so none may appear
    in the rendered rubric — proving they are no longer baked into the template."""
    for profile in (ENGINEER, MEDIC):
        rendered = _render_company_prompt(monkeypatch, profile).lower()
        leaked = [tok for tok in _FORBIDDEN_ANCHORS if tok in rendered]
        assert not leaked, f"foreign anchor(s) leaked for {profile.name}: {leaked}"


# ---------------------------------------------------------------------------
# The dimension DEFINITIONS themselves carry no social-good/NGO worldview
# ---------------------------------------------------------------------------


def test_rendered_engineer_prompt_has_no_worldview_tokens(monkeypatch):
    """Reuses the same WORLDVIEW_TOKEN denylist as the static guard in
    test_no_hardcoded_data.py, checked here against the fully RENDERED prompt
    (post profile-substitution) for an engineer profile — proving a devtools
    company can score high on mission_authenticity without the template
    implying charity work is the reference point."""
    rendered = _render_company_prompt(monkeypatch, ENGINEER)
    hit = WORLDVIEW_TOKEN.search(rendered)
    assert hit is None, f"worldview token leaked into the rendered prompt: {hit.group(0)!r}"


# ===========================================================================
# --- from test_custom_boost_key.py ---
#
# The custom boost emitted by the LLM must reach scoring/tiering.
#
# The bug: the company-scoring prompt asks the LLM for a user-configurable key
# (CUSTOM_BOOST_FIELD, default 'career_narrative_boost'), but the ingestion
# code whitelisted only the legacy 'mpa_narrative_boost'. So a user following
# the docs emitted a boost that the code silently dropped — it never moved the
# tier.
#
# These tests prove:
#   1. the configured key and the prompt's placeholder agree;
#   2. _extract_enrichment preserves the configured boost key;
#   3. _read_custom_boost reads it (and the legacy key for back-compat);
#   4. the boost actually reaches calculate_company_tier (moves the composite).
#
# The source file imported the module as ``score_companies as sc``; renamed
# here to the plain module name ``score_companies`` (already imported at the
# top of this file) to avoid colliding with the ``sc`` DB fixture used
# elsewhere in this file — a pure rename, no behaviour change.
# ===========================================================================


def test_prompt_asks_for_configured_key():
    """The rendered company prompt contains the configured boost key, not a
    stale legacy literal."""
    assert prompts.CUSTOM_BOOST_FIELD == "career_narrative_boost"
    assert prompts.CUSTOM_BOOST_FIELD in prompts.COMPANY_SCORING_PROMPT
    # The placeholder must be fully substituted (no raw {{...}} left).
    assert "{{CUSTOM_BOOST_FIELD}}" not in prompts.COMPANY_SCORING_PROMPT


def test_extract_enrichment_keeps_configured_boost():
    """_extract_enrichment preserves the configured boost key + reasoning."""
    result = {
        "about": {"description": "An org that does good work in the world."},
        "mission_fit": {
            "alignment_score": 70,
            "alignment_label": "Good fit",
            "career_narrative_boost": 80,
            "career_narrative_boost_reasoning": "Strong brand for the CV.",
        },
    }
    enr = score_companies._extract_enrichment(result)
    assert enr is not None
    mf = enr["mission_fit"]
    assert mf["career_narrative_boost"] == 80
    assert mf["career_narrative_boost_reasoning"] == "Strong brand for the CV."


def test_read_custom_boost_configured_key():
    assert score_companies._read_custom_boost({"career_narrative_boost": 65}) == 65


def test_read_custom_boost_legacy_back_compat():
    """Old payloads using the legacy key still resolve."""
    assert score_companies._read_custom_boost({"mpa_narrative_boost": 55}) == 55


def test_read_custom_boost_absent_or_bad():
    assert score_companies._read_custom_boost({}) is None
    assert score_companies._read_custom_boost({"career_narrative_boost": "high"}) is None
    assert score_companies._read_custom_boost(None) is None


def test_boost_reaches_tier_calculation():
    """The extracted boost moves the composite vs. alignment-only."""
    result = {
        "about": {"description": "An org that does good work in the world."},
        "mission_fit": {"alignment_score": 40, "career_narrative_boost": 90},
    }
    enr = score_companies._extract_enrichment(result)
    boost = score_companies._read_custom_boost(enr["mission_fit"])
    assert boost == 90

    tier_no_boost, comp_no_boost = calculate_company_tier(40)
    tier_boost, comp_boost = calculate_company_tier(40, boost)
    # The boost must change the composite (it actually reached the calc).
    assert comp_boost != comp_no_boost
    assert comp_boost > comp_no_boost  # 90 boost lifts a 40 alignment

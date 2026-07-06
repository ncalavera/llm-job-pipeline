"""Company-level dedup: a board-sourced NAME VARIANT of a company we already
track must MERGE into that row, not fork a new candidate.

Two layers:

1. ``company_name_variants_match`` — the pure tolerance matcher. Parametrized
   against the run-log true positives (must merge) and the hand-caught false
   positives (must NOT merge). Precision-biased: normalized-token equality or
   an "ACRONYM - Full Name" containment, nothing looser.

2. ``ensure_company`` merge behavior — SQLite backend, isolated temp DB. An
   existing ACTIVE, already-WANT-scored company plus a board save carrying its
   long-form variant must leave ONE company row (no new candidate), fold the
   variant into ``aliases``, attach the vacancy to the canonical id, and NOT
   touch the existing status / score (no re-enrichment).

Harness mirrors tests/test_dedup_org_whitespace.py (SQLite, per-test temp DB,
reload the DAL chain). Real org names are used because the matcher's behavior
is defined against them in the bug report.
"""

import importlib
import sys

import pytest

from company_registry import company_name_variants_match


# ---------------------------------------------------------------------------
# 1. Pure matcher — must-match / must-not-match quality bar
# ---------------------------------------------------------------------------

MUST_MATCH = [
    (
        "EBRD - European Bank for Reconstruction and Development",
        "european bank for reconstruction and development (ebrd)",
    ),
    ("Save the Children International", "Save the Children"),
    (
        "IFAD - International Fund for Agricultural Development",
        "International Fund for Agricultural Development",
    ),
    ("Code.X 0", "Code.X"),
    ("Resolution", "Resolution Foundation"),
    # Accent folding: NFKD-normalized names must dedup.
    ("Médecins Sans Frontières", "Medecins Sans Frontieres"),
]

MUST_NOT_MATCH = [
    ("Henley & Partners", "Global Partners"),
    ("Via", "[via Fast Forward]"),
    ("Apple", "Apple CSR"),
    ("Imperial College London", "Imperial College London, National Heart and Lung Institute"),
    # Weak generic-token overlap must not merge (prod audit, 2026-07-06).
    ("Frontier Institute of Technology", "Massachusetts Institute of Technology"),
    ("Social Change Lab", "Change.org"),
    # Short/generic existing names as a substring/token of a longer one.
    ("Front", "Frontier Institute of Technology"),
    ("Merge", "Merge Labs"),
    # A distinct joint centre overlaps two DIFFERENT parents — auto-merge into
    # neither (multi-parent ambiguity).
    (
        "Cambridge University, Leverhulme Centre for the Future of Intelligence",
        "University of Cambridge",
    ),
    (
        "Cambridge University, Leverhulme Centre for the Future of Intelligence",
        "Leverhulme Trust",
    ),
    # Anagram is NOT an acronym: in-order initials are FIA, not FAI.
    ("FAI - Fund International Agricultural", "Fund International Agricultural"),
    # A lone generic org-suffix token carries no identity — never merge on it.
    ("The Foundation", "Foundation Inc"),
]


@pytest.mark.parametrize("a,b", MUST_MATCH)
def test_variants_must_match(a, b):
    assert company_name_variants_match(a, b), f"{a!r} should merge into {b!r}"
    assert company_name_variants_match(b, a), "match must be symmetric"


@pytest.mark.parametrize("a,b", MUST_NOT_MATCH)
def test_variants_must_not_match(a, b):
    assert not company_name_variants_match(a, b), f"{a!r} must NOT merge into {b!r}"
    assert not company_name_variants_match(b, a), "non-match must be symmetric"


def test_unrelated_names_do_not_match():
    assert not company_name_variants_match("Open Philanthropy", "GiveWell")
    assert not company_name_variants_match("", "Anything")


# ---------------------------------------------------------------------------
# 2. Merge behavior in the save layer
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "dedup_sweep",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db

    yield db
    db.close_conn()


def _seed_company(dal, canonical, *, status, aliases, score):
    """Insert an existing, already-WANT-scored company directly."""
    import json

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO company (canonical_name, status, aliases, alignment_score, enriched_at)
           VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
        (canonical, status, json.dumps(aliases), score),
    )
    cid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return cid


def _job(title, *, org, city="San Francisco, USA"):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role with real responsibilities.",
        "full_description": f"We are hiring a {title}. " * 12 + "Own the work end to end.",
        "location": city,
        "org_override": org,
    }


def test_board_variant_merges_into_existing_active_company(dal):
    """A board that surfaces "EBRD - European Bank for Reconstruction and
    Development" must NOT create a second row for the existing active "EBRD":
    the variant folds into aliases, the vacancy attaches to the canonical id,
    and the existing status/score are untouched (no re-enrichment)."""
    existing_id = _seed_company(
        dal,
        "EBRD",
        status="active",
        aliases=["european bank for reconstruction and development (ebrd)"],
        score=71,
    )

    board_cfg = {"name": "Impactpool", "url": "https://board.test/feed", "tier": "A"}
    variant = "EBRD - European Bank for Reconstruction and Development"
    dal.save_board_vacancies(board_cfg, [_job("Principal Economist", org=variant)])
    dal.get_conn().commit()

    cur = dal.get_conn().cursor()

    # Exactly one company row — no duplicate candidate.
    cur.execute("SELECT COUNT(*) FROM company")
    (n_companies,) = cur.fetchone()
    assert n_companies == 1, f"expected 1 company, got {n_companies} — variant forked a duplicate"

    # Existing row unchanged (status + score preserved, i.e. not re-scored).
    cur.execute("SELECT id, status, alignment_score, aliases FROM company")
    cid, status, score, aliases = cur.fetchone()
    assert cid == existing_id
    assert status == "active"
    assert score == 71
    # Variant folded into aliases.
    assert variant in aliases, f"variant not folded into aliases: {aliases}"

    # Vacancy attached to the canonical company id.
    cur.execute("SELECT company_id FROM vacancy")
    vac_rows = cur.fetchall()
    assert len(vac_rows) == 1
    assert vac_rows[0][0] == existing_id
    cur.close()


def test_new_org_still_creates_candidate(dal):
    """A genuinely new org (no existing match) still lands as one candidate —
    the merge path must not swallow legitimately new companies."""
    board_cfg = {"name": "Impactpool", "url": "https://board.test/feed", "tier": "A"}
    dal.save_board_vacancies(board_cfg, [_job("Data Scientist", org="Wholly Novel Org")])
    dal.get_conn().commit()

    cur = dal.get_conn().cursor()
    cur.execute("SELECT canonical_name, status FROM company")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Wholly Novel Org"
    assert rows[0][1] == "candidate"
    cur.close()

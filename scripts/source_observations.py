"""Small durable ledger of source listings seen before filtering."""

from __future__ import annotations

import os
from pathlib import Path
from db_backend import IS_SQLITE, sqlite_db_path


def _ledger_connection():
    """Open a short-lived connection so ledger commits cannot sweep caller work."""
    if IS_SQLITE:
        import sqlite3

        conn = sqlite3.connect(str(sqlite_db_path()))
        conn.execute("PRAGMA foreign_keys = ON")
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_observation'"
            ).fetchone()
            is None
        ):
            schema = (
                Path(__file__).resolve().parent.parent
                / "sql"
                / "migrations"
                / "0031_source_observation.sqlite.sql"
            )
            conn.executescript(schema.read_text(encoding="utf-8"))
            conn.commit()
        return conn, "?"
    import psycopg2

    url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")
    return psycopg2.connect(url), "%s"


def record_source_run(
    run_id,
    source_key,
    source_url,
    *,
    raw_count=0,
    accepted_count=0,
    excluded_count=0,
    complete=False,
    error=None,
):
    """Upsert source-run accounting; incomplete runs remain explicitly incomplete."""
    if not run_id:
        return True
    conn, ph = _ledger_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO source_fetch_run
            (run_id, source_key, source_url, raw_count, accepted_count,
             excluded_count, complete, error, completed_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p},
                    CASE WHEN {p} THEN CURRENT_TIMESTAMP ELSE NULL END)
            ON CONFLICT (run_id, source_key) DO UPDATE SET
              source_url=excluded.source_url, raw_count=excluded.raw_count,
              accepted_count=excluded.accepted_count,
              excluded_count=excluded.excluded_count, complete=excluded.complete,
              error=excluded.error, completed_at=excluded.completed_at""".format(p=ph),
            (
                run_id,
                source_key,
                source_url,
                raw_count,
                accepted_count,
                excluded_count,
                complete,
                error,
                complete,
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def record_source_observations(run_id, source_key, source_url, listings):
    """Persist raw listing identity and its filter outcome for one source run."""
    if not run_id or not listings:
        return True
    conn, ph = _ledger_connection()
    try:
        cur = conn.cursor()
        for item in listings:
            cur.execute(
                """INSERT INTO source_observation
                (run_id, source_key, source_url, external_id, title, organization,
                 listing_url, outcome, reason)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT (run_id, source_key, external_id) DO UPDATE SET
                  source_url=excluded.source_url, title=excluded.title,
                  organization=excluded.organization, listing_url=excluded.listing_url,
                  outcome=excluded.outcome, reason=excluded.reason""".format(p=ph),
                (
                    run_id,
                    source_key,
                    source_url,
                    item.get("external_id"),
                    item.get("title"),
                    item.get("organization"),
                    item.get("url"),
                    item.get("outcome", "accepted"),
                    item.get("reason"),
                ),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def recent_source_runs(limit=20):
    """Return newest source accounting rows, or [] when the ledger is unavailable."""
    conn = None
    try:
        conn, ph = _ledger_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT source_key, run_id, complete, completed_at, created_at,
                      raw_count, accepted_count, excluded_count, error
               FROM source_fetch_run ORDER BY created_at DESC LIMIT {p}""".format(p=ph),
            (min(int(limit), 20),),
        )
        rows = cur.fetchall()
        cur.close()

        def stamp(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        return [
            {
                "source": r[0],
                "run_id": r[1],
                "status": "complete" if r[2] else "incomplete",
                "completed_at": stamp(r[3]),
                "started_at": stamp(r[4]),
                "raw_count": r[5],
                "accepted_count": r[6],
                "excluded_count": r[7],
                "error": r[8],
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def lookup_source_url(source_key, external_id):
    """Return the latest known source URL for a source listing."""
    conn = None
    try:
        conn, ph = _ledger_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT listing_url FROM source_observation WHERE source_key = {p} "
            "AND external_id = {p} ORDER BY observed_at DESC LIMIT 1".format(p=ph),
            (source_key, external_id),
        )
        result = cur.fetchone()
        cur.close()
        return result[0] if result else None
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def lookup_source_urls(source_keys, external_ids):
    """Return known listing URLs with one ledger connection/query."""
    keys = [str(k) for k in source_keys if k]
    ids = [str(i) for i in external_ids if i]
    if not keys or not ids:
        return {}
    conn = None
    try:
        conn, ph = _ledger_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT external_id, listing_url FROM source_observation "
            f"WHERE source_key IN ({','.join([ph] * len(keys))}) "
            f"AND external_id IN ({','.join([ph] * len(ids))}) "
            "ORDER BY observed_at DESC",
            tuple(keys + ids),
        )
        out = {}
        for external_id, listing_url in cur.fetchall():
            out.setdefault(str(external_id), listing_url)
        cur.close()
        return out
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def record_import_outcome(cur, job, outcome, reason=None, canonical_id=None):
    """Stage import accounting in the same transaction as the vacancy write."""
    if not job.get("_source_run"):
        return
    cur.execute(
        """UPDATE source_observation SET outcome = %s,
           reason = COALESCE(%s, reason), canonical_id = COALESCE(%s, canonical_id)
           WHERE run_id = %s AND source_key = %s AND external_id = %s""",
        (
            outcome,
            reason,
            str(canonical_id) if canonical_id else None,
            job["_source_run"],
            job["_source_key"],
            str(job.get("external_id", "")),
        ),
    )

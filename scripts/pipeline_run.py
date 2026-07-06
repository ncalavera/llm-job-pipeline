"""Durable per-run history — the ``pipeline_run`` table.

``run_card.py`` shows a run WHILE it happens; this module records it so a run can
be reviewed AFTER it finishes (or dies). ``run_daily.record()`` calls
:func:`record` at run start and after every stage transition, upserting ONE row
per run: run-level status/counts/timing plus a compact per-stage snapshot. A
finished or killed run therefore leaves a durable trace once the terminal
scrollback is gone.

Best-effort, exactly like ``run_status.py``: a history write must NEVER break the
daily run (STRATEGY goal 1). Every DB touch is wrapped; a failure (table missing
on a not-yet-migrated DB, outage, unexpected shape) never raises — after a
rollback so it can't poison the driver's shared connection. But NOT silently:
best-effort with zero trace would defeat the point (history would just never
accumulate, and the health check would say "no baseline yet" forever with no
hint why), so the first failure per process prints ONE stderr warning naming the
exception. The read helper :func:`recent_new_vacancies` feeds the end-of-run
expected-range check and likewise degrades to an empty history.
"""

from __future__ import annotations

import sys

# One warning per process: enough to make a broken history loud without spamming
# a line after every stage transition of the same run.
_warned = False


def _warn_once(op: str, exc: Exception) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"  ⚠  pipeline_run history {op} failed ({type(exc).__name__}: {exc}) — "
        "the run continues, but no durable history accumulates until this is fixed "
        "(missing table? run scripts/migrate.py).",
        file=sys.stderr,
        flush=True,
    )


def _status_of(state: dict) -> str:
    """Coarse run status derived from the stage board — persisted for later review."""
    if state.get("finished"):
        return "done"
    statuses = [s.get("status") for s in state.get("stages", [])]
    if "error" in statuses:
        return "error"
    if "aborted" in statuses:
        return "aborted"
    if state.get("gate"):
        return "gate"
    return "running"


def _stage_row(s: dict) -> dict:
    """The reviewable subset of a stage entry (drop the bulky live gate payload)."""
    return {
        "name": s.get("name"),
        "status": s.get("status"),
        "note": s.get("note"),
        "started_at": s.get("started_at"),
        "finished_at": s.get("finished_at"),
    }


def record(state: dict, boards: str | None = None, counts: dict | None = None) -> None:
    """Upsert this run's row from a live ``run_state`` dict. Best-effort, no raise.

    Called at run start and after every stage transition; the first call INSERTs,
    later calls UPDATE the same ``run_id`` (UPDATE-then-INSERT-on-miss, so it
    needs no ON CONFLICT / unique constraint and works on both backends)."""
    run_id = state.get("run_id")
    if not run_id:
        return
    try:
        from db_backend import Json
        from db_conn import get_conn
    except Exception:
        return

    counts = counts or {}
    payload = {
        "finished": bool(state.get("finished")),
        "status": _status_of(state),
        "boards": boards,
        "new_vacancies": counts.get("new_vacancies"),
        "scored": counts.get("scored"),
        "companies_scored": counts.get("companies_scored"),
        "stages": Json([_stage_row(s) for s in state.get("stages", [])]),
        "counts": Json(counts),
        "errors": Json(
            {
                s.get("name"): s.get("note")
                for s in state.get("stages", [])
                if s.get("status") == "error"
            }
        ),
    }
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE pipeline_run SET updated_at = now(), finished = %s, status = %s, "
            "boards = %s, new_vacancies = %s, scored = %s, companies_scored = %s, "
            "stages = %s, counts = %s, errors = %s WHERE run_id = %s",
            (
                payload["finished"],
                payload["status"],
                payload["boards"],
                payload["new_vacancies"],
                payload["scored"],
                payload["companies_scored"],
                payload["stages"],
                payload["counts"],
                payload["errors"],
                run_id,
            ),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO pipeline_run (run_id, run_at, updated_at, finished, status, "
                "boards, new_vacancies, scored, companies_scored, stages, counts, errors) "
                "VALUES (%s, now(), now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    payload["finished"],
                    payload["status"],
                    payload["boards"],
                    payload["new_vacancies"],
                    payload["scored"],
                    payload["companies_scored"],
                    payload["stages"],
                    payload["counts"],
                    payload["errors"],
                ),
            )
        conn.commit()
        cur.close()
    except Exception as exc:
        # A history write must never break the run; undo the partial write so the
        # driver's shared connection is not left in a poisoned transaction — but
        # say so once, or the dead-history bug quietly comes back as an always-empty history.
        _warn_once("write", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass


def recent_new_vacancies(limit: int = 10, exclude_run_id: str | None = None) -> list[int]:
    """The ``new_vacancies`` counter of the last ``limit`` FINISHED runs (newest
    first), excluding ``exclude_run_id`` (this run). Feeds the expected-range
    check. Returns ``[]`` on any failure (warned once) — no history, no verdict."""
    try:
        from db_conn import get_conn

        cur = get_conn().cursor()
        cur.execute(
            "SELECT new_vacancies FROM pipeline_run "
            "WHERE finished = %s AND new_vacancies IS NOT NULL AND run_id <> %s "
            "ORDER BY run_at DESC LIMIT %s",
            (True, exclude_run_id or "", int(limit)),
        )
        rows = cur.fetchall()
        cur.close()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception as exc:
        _warn_once("read", exc)
        return []

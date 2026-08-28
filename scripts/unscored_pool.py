"""The ONE definition of "a role waiting to be scored".

Two stages report this number to a human — the filter stage's note and the
morning digest header — and they used to compute it separately and disagree
(2026-08-28: the filter said 26, the digest said 20, same minute, same
database). Both now count with the SQL below, so there is one definition to
argue with instead of two numbers to reconcile.

A role waits to be scored when all three hold:

  * it has no score yet — ``llm_score`` NULL, or the negative failure
    sentinel, matching ``load_vacancies(unscored_only=True)`` and the
    dashboard's unscored count;
  * its status is ``unseen`` — the scorer refuses ``passed`` / ``skipped``
    rows (``score_vacancies.py`` status_exclude), and ``liked`` / ``applied``
    / ``archived`` rows are already decided, so none of them is waiting;
  * the filter pass has not excluded it — ``scoring_excluded_reason`` is NULL
    (migration 0025). A row with a reason is dropped, not waiting.

The count is split by COMPANY status, because that is the difference between
a role that the next run will score and one parked out of sight:

  * ``active``    — the scoring pool proper.
  * ``candidate`` — parked behind a company nobody has approved yet. The
    capped candidate rescue (``load_candidate_vacancies_for_scoring``) takes a
    few per run; the rest simply wait. Reported as its own labelled number so
    a large parked backlog can never hide behind a small "waiting" figure.
  * ``other``     — inactive companies; their roles are not offered at all.

This module deliberately imports nothing: the digest also runs on a bare host
with no project tree, so it must stay as cheap to import as a constant.
"""

#: The predicate. ``v`` is the vacancy alias; join ``company`` as ``c``.
UNSCORED_POOL_WHERE = """
    (v.llm_score IS NULL OR v.llm_score < 0)
    AND v.status = 'unseen'
    AND v.scoring_excluded_reason IS NULL
"""

COUNT_BY_COMPANY_STATUS_SQL = f"""
SELECT c.status AS company_status, count(*) AS n
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE {UNSCORED_POOL_WHERE}
GROUP BY c.status
"""


def counts(cur) -> dict:
    """``{"active": N, "candidate": M, "other": K}`` — every key always present.

    Takes an already-open cursor (dict-row or tuple-row, psycopg2 or the
    SQLite shim) so the caller keeps ownership of its own connection.
    """
    cur.execute(COUNT_BY_COMPANY_STATUS_SQL)
    out = {"active": 0, "candidate": 0, "other": 0}
    for row in cur.fetchall():
        try:
            status, n = row["company_status"], row["n"]
        except (TypeError, KeyError, IndexError):
            status, n = row[0], row[1]
        out[status if status in ("active", "candidate") else "other"] += int(n or 0)
    return out

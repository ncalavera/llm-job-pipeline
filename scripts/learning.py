"""The learning cycle — verdicts teach filters, scoring and boards.

STRATEGY guardrail 8 ("closed feedback loops, never silent self-edits"): every
judging surface has a path from the user's verdicts back to PROPOSED corrections,
offered at the start of the next run (skippable; skipped verdicts roll over).
This module owns the DETERMINISTIC mechanics of that loop — proposal
computation, the backtest, the rollover cursor, the applied-changes log, and the
filter-kill revision. The agent (the /jobs-new skill) supplies judgment at the
gate: it shows the proposals, captures the user's yes/no, and calls back in to
apply the approved ones. Nothing here edits a filter, the profile or the board
set without an explicit apply call, and every applied change is logged.

Split of concerns:
  * PURE functions (backtest, propose_*, culprit_rule) decide on plain data and
    are unit-tested directly — no DB, no clock, no LLM.
  * DB helpers read verdicts + write the append-only ``learning_log`` ledger.
  * Profile editors apply an approved move to the active profile file.
  * ``scoring_agreement()`` is the ONE integration point for the golden-set eval
    harness, with a graceful "not yet measured" fallback.

Zero LLM spend: the whole cycle is string/rule matching against history.
"""

from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Vocabulary — verdict baskets (mirrors CONCEPTS.md "liked basket").
# ---------------------------------------------------------------------------

#: Statuses that mean active interest (a "liked" verdict, in the broad sense).
LIKED_BASKET = ("liked", "to_apply", "to_research", "to_network", "applied", "interview")
#: The employer said no. NOT a user verdict — he wanted this role, someone else
#: closed it. Kept out of ``DECISION_STATUSES`` so it never reads as "he passed",
#: but folded into the backtest reference set below: a role he applied to is
#: proof of fit, so a filter word that would have killed it is dirty.
REJECTED_STATUSES = ("declined",)
#: A verdict the user actually MADE (a decision) — liked basket plus an explicit
#: pass. ``skipped``/``unseen`` are deferrals, not verdicts.
DECISION_STATUSES = LIKED_BASKET + ("passed",)
#: Statuses proving the user wanted the role, whoever decided afterwards. This
#: is the backtest reference set — wider than DECISION_STATUSES on purpose.
WANTED_STATUSES = LIKED_BASKET + REJECTED_STATUSES

#: A candidate filter word must recur in at least this many garbage titles before
#: it is even worth backtesting — one fluke should never propose a filter.
MIN_GARBAGE_HITS = 2
#: Vacancies at/above this score are "good enough that a filter must not kill
#: them" — the backtest reference set alongside liked history (ticket: ">= 40").
BACKTEST_MIN_SCORE = 40
#: A board is only worth proposing to disable once it has produced at least this
#: many roles with zero good ones (avoids nuking a board on a single dud).
MIN_BOARD_SEEN = 10
#: Revision cadence — offer the filter-kill sample at most this often.
REVISION_INTERVAL_DAYS = 7
#: Words too generic to ever propose as a filter (would over-kill).
_STOPWORDS = frozenset(
    """a an the of and or to for in on at by with from into as is are be role roles
    job jobs position positions officer manager lead senior junior head director
    specialist coordinator associate assistant analyst intern our your you we team
    global remote hybrid part full time""".split()
)
_WORD = re.compile(r"[a-z][a-z+/&.-]*[a-z]|[a-z]", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Backend-agnostic ledger (append-only). Reused idioms: %s placeholders + now();
# the SQLite adapter translates both (see db_backend). Json() wraps the JSON
# column so it lands as JSONB on Postgres and JSON text on SQLite.
# ---------------------------------------------------------------------------


def _conn():
    from db_conn import get_conn

    return get_conn()


def _is_sqlite() -> bool:
    from db_conn import IS_SQLITE

    return IS_SQLITE


def table_ready() -> bool:
    """True when the ``learning_log`` ledger exists. False means migrate.py has
    not run yet; the caller degrades gracefully instead of crashing the run."""
    cur = _conn().cursor()
    try:
        if _is_sqlite():
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='learning_log'")
            return cur.fetchone() is not None
        cur.execute("SELECT to_regclass('public.learning_log')")
        row = cur.fetchone()
        return bool(row and row[0] is not None)
    except Exception:
        return False
    finally:
        cur.close()


def _require_ledger() -> None:
    """Refuse to apply a self-edit we could not log (STRATEGY guardrail 8).

    The ``apply_*`` helpers mutate DURABLE state — the profile file or the board
    ``enabled`` flag — and THEN append an 'applied' row to the ``learning_log``
    ledger. If that table is missing (migrate.py never ran) the append fails
    AFTER the edit: the change is applied but unlogged, and a raw ``no such
    table: learning_log`` traceback surfaces to the user — a silent, unlogged
    self-edit, the exact thing the guardrail forbids. Checking the ledger is
    ready up front, BEFORE touching anything, makes the failure loud and atomic
    (nothing changed) instead of half-applied.
    """
    if not table_ready():
        raise RuntimeError(
            "learning_log ledger is missing — run `python3 scripts/migrate.py` "
            "before applying a learning-cycle change. Nothing was modified."
        )


def log_event(kind: str, ref: str | None = None, detail: dict | None = None) -> None:
    """Append one row to the ledger and commit (DAL writes are not auto-committed)."""
    from db_backend import Json

    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO learning_log (kind, ref, detail) VALUES (%s, %s, %s)",
        (kind, ref, Json(detail or {})),
    )
    cur.close()
    conn.commit()


def _detail(raw) -> dict:
    """Normalize a ledger detail value — dict on Postgres (JSONB), str on SQLite."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return {}


def cursor_ts():
    """The rollover cursor: the timestamp of the latest COMPLETED review.

    Verdicts decided after this are the undiscussed ones. ``None`` (no review
    ever) means everything is undiscussed. A skipped review writes no 'reviewed'
    row, so the cursor does not move and its verdicts roll over."""
    cur = _conn().cursor()
    cur.execute("SELECT max(created_at) FROM learning_log WHERE kind = 'reviewed'")
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def decided_since_cursor() -> int:
    """How many verdicts the user has made since the last review (rolls over)."""
    ts = cursor_ts()
    placeholders = ", ".join(["%s"] * len(DECISION_STATUSES))
    sql = f"SELECT count(*) FROM vacancy WHERE status IN ({placeholders})"
    params: list = list(DECISION_STATUSES)
    if ts is not None:
        sql += " AND status_updated_at > %s"
        params.append(ts)
    cur = _conn().cursor()
    cur.execute(sql, params)
    n = cur.fetchone()[0]
    cur.close()
    return int(n or 0)


def undiscussed_garbage() -> list[dict]:
    """Garbage verdicts (filter holes) recorded since the last review.

    Returns [{vacancy_id, title, source, score}]. These drive filter-word
    proposals — distinct from a plain pass ('not mine'), which stays a vacancy
    status and calibrates scoring."""
    ts = cursor_ts()
    sql = "SELECT ref, detail FROM learning_log WHERE kind = 'garbage'"
    params: list = []
    if ts is not None:
        sql += " AND created_at > %s"
        params.append(ts)
    cur = _conn().cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    out = []
    for ref, detail in rows:
        d = _detail(detail)
        out.append(
            {
                "vacancy_id": ref,
                "title": d.get("title", ""),
                "source": d.get("source", ""),
                "score": d.get("score"),
            }
        )
    return out


def record_garbage(vacancy_id: str, title: str, source: str = "", score=None) -> None:
    """Record a 'garbage' verdict — the role reached scoring but was junk (a
    filter hole). Denormalized (title/source/score copied) so the signal
    survives even if the vacancy is later archived."""
    log_event("garbage", ref=vacancy_id, detail={"title": title, "source": source, "score": score})


def record_applied(kind: str, detail: dict) -> None:
    """Log an applied change (STRATEGY guardrail 8: every change is logged)."""
    log_event("applied", ref=kind, detail=detail)


def mark_reviewed(agreement=None, applied_count: int = 0, revision_shown: bool = False) -> None:
    """Close a review cycle: advance the rollover cursor. Called ONLY when the
    user engaged (applied or explicitly finished) — NOT on skip, so a skipped
    review's verdicts roll over to next time."""
    log_event(
        "reviewed",
        detail={
            "agreement": agreement,
            "applied_count": applied_count,
            "revision_shown": revision_shown,
        },
    )


def applied_log(limit: int = 20) -> list[dict]:
    """Recent applied changes — the audit trail."""
    cur = _conn().cursor()
    cur.execute(
        "SELECT ref, detail, created_at FROM learning_log WHERE kind = 'applied' "
        "ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    return [{"kind": r[0], "detail": _detail(r[1]), "at": str(r[2])} for r in rows]


# ---------------------------------------------------------------------------
# Verdict-history reads (the backtest reference sets).
# ---------------------------------------------------------------------------


def _titles_where(where: str, params: tuple = ()) -> list[str]:
    cur = _conn().cursor()
    cur.execute(f"SELECT title FROM vacancy WHERE {where}", params)
    rows = cur.fetchall()
    cur.close()
    return [r[0] for r in rows if r[0]]


def liked_titles() -> list[str]:
    """Titles the user wanted — the backtest reference set. Includes roles he
    applied to and was declined for: the employer's no does not make the role a
    bad fit, so a filter word that would have killed it is still dirty."""
    placeholders = ", ".join(["%s"] * len(WANTED_STATUSES))
    return _titles_where(f"status IN ({placeholders})", tuple(WANTED_STATUSES))


def high_scored_titles(min_score: int = BACKTEST_MIN_SCORE) -> list[str]:
    return _titles_where(
        "llm_score IS NOT NULL AND llm_score >= %s AND status != 'archived'", (min_score,)
    )


def last_reviewed_detail() -> dict | None:
    cur = _conn().cursor()
    cur.execute(
        "SELECT detail FROM learning_log WHERE kind = 'reviewed' ORDER BY created_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    cur.close()
    return _detail(row[0]) if row else None


# ---------------------------------------------------------------------------
# PURE proposal logic — no DB, no clock. Unit-tested directly.
# ---------------------------------------------------------------------------


def word_matches_title(word: str, title: str) -> bool:
    """Would the LIVE title filter (scripts/filters.py) kill this title if
    ``word`` were added to ``exclude_title_keywords``?

    Reuses filters.build_title_blacklist_pattern — the exact function that
    compiles the real ``_TITLE_BLACKLIST_PATTERN`` — instead of a hand-rolled
    regex, so the backtest cannot silently drift from what the live filter
    actually does (e.g. its "es"-or-"s" plural handling: "coach" also matches
    "Coaches").

    Scope: this models ONLY the title-keyword filter in scripts/filters.py.
    It does NOT model scripts/fetchers/parsing.py::_blacklist_filter (used by
    board fetchers), which matches the same GLOBAL_BLACKLIST words but with a
    plain ``\\bword\\b`` — no plural handling — a different rule set.
    """
    if not word or not title:
        return False
    from filters import build_title_blacklist_pattern

    pat = build_title_blacklist_pattern([word.strip().lower()])
    return bool(pat.search(title))


def backtest_filter_word(word: str, liked: list[str], high: list[str]) -> dict:
    """Would adding ``word`` to the title filter have killed anything good?

    "Clean" = the word matches NO liked-history title AND no title of a vacancy
    scored >= 40. A clean word is safe to propose; a dirty one lists the exact
    titles it would have wrongly killed (the collisions). Deterministic string
    matching — no LLM.
    """
    collisions = []
    seen = set()
    for t in list(liked) + list(high):
        if t in seen:
            continue
        seen.add(t)
        if word_matches_title(word, t):
            collisions.append(t)
    return {
        "word": word.strip().lower(),
        "clean": not collisions,
        "checked_liked": len(liked),
        "checked_high": len(high),
        "collisions": collisions,
    }


def _tokenize_title(title: str) -> list[str]:
    words = [w.lower() for w in _WORD.findall(title or "")]
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS]


def propose_filter_words(
    garbage_titles: list[str],
    liked: list[str],
    high: list[str],
    existing: list[str] | None = None,
    min_hits: int = MIN_GARBAGE_HITS,
    max_props: int = 5,
) -> dict:
    """From garbage titles, propose title words to add to the filter.

    A word must recur across >= ``min_hits`` distinct garbage titles and pass a
    clean backtest against liked history + high-scored roles. Returns both the
    ``proposals`` (clean, each carrying its backtest) and the ``rejected``
    candidates (with the collisions that block them), so the screen can always
    SHOW the backtest result — accepted or not.
    """
    existing_set = {e.strip().lower() for e in (existing or [])}
    counts: dict[str, int] = {}
    for title in garbage_titles:
        for w in set(_tokenize_title(title)):
            counts[w] = counts.get(w, 0) + 1

    proposals, rejected = [], []
    for word, hits in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if hits < min_hits or word in existing_set:
            continue
        bt = backtest_filter_word(word, liked, high)
        entry = {"word": word, "garbage_hits": hits, "backtest": bt}
        if bt["clean"]:
            proposals.append(entry)
        else:
            rejected.append(entry)
    return {"proposals": proposals[:max_props], "rejected": rejected[:max_props]}


def propose_board_disables(
    vacancies: list[dict], known_boards: set[str], min_seen: int = MIN_BOARD_SEEN
) -> list[dict]:
    """Propose disabling a board that produced many roles and zero good ones.

    ``vacancies`` are dicts with a ``status`` and a per-source provenance
    (top-level ``source`` and/or ``locations[].source``). Only recognized board
    ids are ever proposed. Pure: caller supplies the loaded vacancies.
    """
    seen: dict[str, int] = {}
    good: dict[str, int] = {}
    for vac in vacancies:
        srcs = set()
        if vac.get("source"):
            srcs.add(str(vac["source"]))
        for loc in vac.get("locations", []) or []:
            if isinstance(loc, dict) and loc.get("source"):
                srcs.add(str(loc["source"]))
        # A board that surfaced a role he applied to has earned its keep, even
        # if the employer said no — hence WANTED_STATUSES, not LIKED_BASKET.
        is_good = vac.get("status") in WANTED_STATUSES
        for s in srcs:
            if s not in known_boards:
                continue
            seen[s] = seen.get(s, 0) + 1
            if is_good:
                good[s] = good.get(s, 0) + 1
    out = []
    for board, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= min_seen and good.get(board, 0) == 0:
            out.append({"board": board, "seen": n, "good": 0})
    return out


def propose_factor_moves(
    penalty_factors: list[str],
    garbage_titles: list[str],
    liked: list[str],
    high: list[str],
    min_hits: int = MIN_GARBAGE_HITS,
) -> list[dict]:
    """Propose PROMOTING a penalty factor to a hard filter (penalty -> filter).

    A penalty that keeps showing up in garbage titles and has NEVER collided with
    a liked/high-scored role is a hard block in disguise: promoting it stops
    paying to score those roles. We derive a candidate keyword from the penalty
    text, require it to recur in garbage, and require a clean backtest. The loop
    only PROPOSES — the move needs the user's yes (see apply_factor_move).
    """
    out = []
    for factor in penalty_factors:
        for word in set(_tokenize_title(factor)):
            hits = sum(1 for t in garbage_titles if word_matches_title(word, t))
            if hits < min_hits:
                continue
            bt = backtest_filter_word(word, liked, high)
            if bt["clean"]:
                out.append(
                    {
                        "factor": factor,
                        "keyword": word,
                        "from": "penalty",
                        "to": "filter",
                        "garbage_hits": hits,
                        "backtest": bt,
                    }
                )
                break  # one representative keyword per factor is enough
    return out


# ---------------------------------------------------------------------------
# Filter-kill revision — "anything alive here?"
# ---------------------------------------------------------------------------


def _archive_dir() -> Path:
    override = os.environ.get("LEARNING_ARCHIVE_DIR")
    if override:
        return Path(override)
    from config import VACANCIES_DIR

    return Path(VACANCIES_DIR) / "archive"


def personal_filter_words() -> list[str]:
    """The user's own title filter words (the movable/weakenable rules). The
    universal junk words are shipped-neutral and are NOT user rules, so they are
    excluded — the revision can only weaken what the user chose. Read via the
    hard-filter loader (not config's import-time cache) so a same-process apply is
    reflected immediately."""
    try:
        from hard_filters import load_hard_filters

        kws = load_hard_filters().get("exclude_title_keywords", [])
        return [w.strip().lower() for w in kws if w and w.strip()]
    except Exception:
        return []


def culprit_rule(title: str, personal_filters: list[str]) -> str | None:
    """Which personal filter word killed this title (the rule to weaken). None
    when only universal junk / geo matched — those are not user rules."""
    for word in personal_filters:
        if word_matches_title(word, title):
            return word
    return None


def sample_filter_kills(n: int = 10, personal_filters: list[str] | None = None) -> list[dict]:
    """Sample up to ``n`` recently filter-killed titles for the "anything alive
    here?" revision. Reads the filter delete-archives (gitignored user space).
    Each item carries the personal culprit rule when there is one to weaken.
    """
    if personal_filters is None:
        personal_filters = personal_filter_words()
    archive_dir = _archive_dir()
    if not archive_dir.exists():
        return []
    items: list[dict] = []
    seen_titles: set[str] = set()
    for path in sorted(archive_dir.glob("filter_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for vid, vac in (data.get("vacancies", {}) or {}).items():
            title = vac.get("title", "") or ""
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            items.append(
                {
                    "vacancy_id": vid,
                    "title": title,
                    "org": vac.get("org", ""),
                    "culprit": culprit_rule(title, personal_filters),
                }
            )
    if len(items) <= n:
        return items
    return random.sample(items, n)


def revision_due() -> bool:
    """True when it is time to show the filter-kill revision (~weekly).

    The cadence clock is the created_at of the most recent COMPLETED review that
    actually SHOWED the revision. We filter that flag in Python (not in SQL): the
    detail column is JSONB on Postgres and JSON text on SQLite, so a portable
    ``LIKE`` is impossible — reading the rows and inspecting the parsed detail is
    the dialect-safe way."""
    cur = _conn().cursor()
    cur.execute(
        "SELECT detail, created_at FROM learning_log WHERE kind = 'reviewed' "
        "ORDER BY created_at DESC LIMIT 30"
    )
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return True  # never reviewed → offer it
    for detail, created in rows:
        if _detail(detail).get("revision_shown"):
            return _days_since(str(created)) >= REVISION_INTERVAL_DAYS
    return True  # reviewed before, but the revision has never been shown → due


def _days_since(ts: str) -> float:
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts[:19], fmt)
            return (datetime.now() - dt).total_seconds() / 86400.0
        except ValueError:
            continue
    return 999.0


# ---------------------------------------------------------------------------
# Profile editors — apply an approved change to the ACTIVE profile file.
# Surgical: touch only the target line/bullet, preserve everything else.
# ---------------------------------------------------------------------------

_EMPTY_TOKENS = {"", "(none)", "none", "-", "n/a", "na"}
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"


def _profile_path() -> Path:
    env = os.environ.get("USER_PROFILE_PATH")
    if env:
        return Path(env).expanduser()
    from prompts import DEFAULT_PROFILE_PATH

    return DEFAULT_PROFILE_PATH


def _iter_lines_outside_comments(lines: list[str]):
    """Yield (index, line, in_section_of) skipping HTML-comment blocks so an
    example inside a ``<!-- -->`` template is never mistaken for a real value."""
    in_comment = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_comment and _COMMENT_OPEN in line and _COMMENT_CLOSE not in line:
            in_comment = True
            yield i, line, False
            continue
        if in_comment:
            if _COMMENT_CLOSE in line:
                in_comment = False
            yield i, line, False
            continue
        yield i, line, True


def _edit_title_keyword_line(text: str, transform) -> tuple[str, list[str]]:
    """Apply ``transform(list)->list`` to the real exclude_title_keywords line.
    Returns (new_text, new_list). Creates the line if the section exists but the
    line is absent."""
    lines = text.splitlines()
    field = "exclude_title_keywords"
    target_idx = None
    for i, line, real in _iter_lines_outside_comments(lines):
        if real and line.strip().lower().startswith(field + ":"):
            target_idx = i
            break

    if target_idx is not None:
        raw = lines[target_idx].split(":", 1)[1]
        current = _split_csv(raw)
    else:
        current = []
    new_list = transform(list(current))
    rendered = ", ".join(new_list) if new_list else "(none)"
    new_line = f"{field}: {rendered}"

    if target_idx is not None:
        lines[target_idx] = new_line
    else:
        # Insert under the ## HARD_FILTERS header if present, else append.
        hdr = _find_section_header(lines, "HARD_FILTERS")
        if hdr is not None:
            lines.insert(hdr + 1, new_line)
        else:
            lines += ["", "## HARD_FILTERS", new_line]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), new_list


def _split_csv(value: str) -> list[str]:
    out = []
    for raw in (value or "").split(","):
        tok = raw.strip()
        if tok and tok.lower() not in _EMPTY_TOKENS:
            out.append(tok.lower())
    return out


def _find_section_header(lines: list[str], name: str) -> int | None:
    for i, line in enumerate(lines):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and m.group(1).upper().replace(" ", "_") == name:
            return i
    return None


def _remove_penalty_bullet(text: str, bullet_text: str) -> tuple[str, bool]:
    """Remove one bullet from ## EXCLUDE_PATTERNS by exact (trimmed) match."""
    lines = text.splitlines()
    start = _find_section_header(lines, "EXCLUDE_PATTERNS")
    if start is None:
        return text, False
    target = bullet_text.strip()
    for i in range(start + 1, len(lines)):
        if re.match(r"^##\s+", lines[i]):
            break
        m = re.match(r"^\s*[-*]\s+(.*\S)\s*$", lines[i])
        if m and m.group(1).strip() == target:
            del lines[i]
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True
    return text, False


def _refresh_config():
    """The filter caches (config.EXCLUDE_TITLE_KEYWORDS, GLOBAL_BLACKLIST) are
    built once at import. After editing the profile, drop the profile cache so a
    later read in the SAME process sees the change. Fetch/filter run as fresh
    subprocesses (they re-import config), so the persisted file is what matters;
    this keeps an in-process caller consistent too."""
    try:
        from prompts import clear_profile_cache

        clear_profile_cache()
    except Exception:
        pass


def _atomic_write_profile(path: Path, new_text: str, previous_text: str) -> None:
    """Snapshot the pre-edit profile to a sibling ``.bak`` then atomically
    replace the live file — tmp file in the same directory + os.replace, the
    project's existing idiom (see run_daily.py's ``_save_state``,
    dedup_sweep.py's ``_archive_losers``). ``config/user_profile.md`` is
    gitignored and un-versioned, so this is its only safety net.

    If ``os.replace`` raises (e.g. a crash mid-write), the live file at
    ``path`` was never touched — only the tmp file — so the original content
    survives.
    """
    backup = path.with_name(path.name + ".bak")
    backup.write_text(previous_text, encoding="utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)


def apply_add_filter_word(word: str) -> dict:
    """Add a title keyword to the hard filter (approved 'add word W'). Logged."""
    _require_ledger()
    word = word.strip().lower()
    path = _profile_path()
    text = path.read_text(encoding="utf-8")
    new_text, new_list = _edit_title_keyword_line(
        text, lambda lst: lst if word in lst else lst + [word]
    )
    _atomic_write_profile(path, new_text, text)
    _refresh_config()
    record_applied("add_filter_word", {"word": word, "result_list": new_list})
    return {"word": word, "filters_now": new_list}


def apply_remove_filter_word(word: str) -> dict:
    """Remove a title keyword from the hard filter (weaken a rule). Logged."""
    _require_ledger()
    word = word.strip().lower()
    path = _profile_path()
    text = path.read_text(encoding="utf-8")
    new_text, new_list = _edit_title_keyword_line(text, lambda lst: [w for w in lst if w != word])
    _atomic_write_profile(path, new_text, text)
    _refresh_config()
    record_applied("weaken_filter_word", {"word": word, "result_list": new_list})
    return {"word": word, "filters_now": new_list}


def apply_factor_move(factor_text: str, keyword: str) -> dict:
    """Move a factor penalty -> filter: drop the EXCLUDE_PATTERNS bullet and add
    the keyword to the hard filter. Both edits, then log. Never called without an
    explicit user yes."""
    _require_ledger()
    path = _profile_path()
    original = path.read_text(encoding="utf-8")
    text, removed = _remove_penalty_bullet(original, factor_text)
    new_text, new_list = _edit_title_keyword_line(
        text, lambda lst: lst if keyword.strip().lower() in lst else lst + [keyword.strip().lower()]
    )
    _atomic_write_profile(path, new_text, original)
    _refresh_config()
    record_applied(
        "move_factor",
        {
            "factor": factor_text,
            "keyword": keyword.strip().lower(),
            "from": "penalty",
            "to": "filter",
            "penalty_removed": removed,
            "filters_now": new_list,
        },
    )
    return {"factor": factor_text, "keyword": keyword.strip().lower(), "filters_now": new_list}


def apply_disable_board(board: str) -> dict:
    """Apply an approved 'disable board' decision: clear the board's persisted
    ``enabled`` flag so it stops participating in future runs, and log the
    decision (the audit trail). This closes the loop — the proposal
    (propose_board_disables) → user approval → this apply — instead of only
    printing advice. It changes just the durable default; the board can still be
    re-enabled for a one-off run via JOB_BOARDS / --boards. Applied only on the
    user's explicit yes (guardrail 8: never a silent self-edit)."""
    _require_ledger()
    from database_supabase import set_board_enabled

    set_board_enabled(board, False)
    record_applied("disable_board", {"board": board})
    return {"board": board, "note": "disabled — removed from the persisted board set"}


# ---------------------------------------------------------------------------
# Scoring-agreement adapter — the ONE integration point for the golden-set eval
# harness (scripts/golden_set.py). The gate stays zero-LLM: this module never
# computes its own agreement number, it reads the LAST measurement the harness
# persisted (``python3 scripts/golden_set.py measure`` writes a small summary
# JSON next to the golden set — see golden_set.write_summary /
# scoring_agreement_summary) via this small, clearly-marked seam, and falls
# back to "not yet measured" until you run it at least once. A provider can
# wire in by exposing a module with EITHER:
#   * ``scoring_agreement_summary() -> dict | None`` — {agreement_pct,
#     measured_at, ...}, preferred (carries the measured-at date), OR
#   * ``scoring_agreement_pct() -> float | None`` (0-100), no timestamp.
# The env var LEARNING_AGREEMENT_CMD (a shell command printing the percentage
# on stdout) remains an escape hatch that bypasses both.
# ---------------------------------------------------------------------------

_AGREEMENT_PROVIDERS = ("golden_eval", "golden_set", "score_eval")


def _measure_agreement():
    """Returns (value, source, measured_at). ``value`` is a 0-100 percentage or
    None; ``measured_at`` is an ISO timestamp when the provider carries one
    (the golden-set summary does; the LEARNING_AGREEMENT_CMD escape hatch does
    not)."""
    cmd = os.environ.get("LEARNING_AGREEMENT_CMD")
    if cmd:
        import subprocess

        try:
            out = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120
            ).stdout
            m = re.search(r"-?\d+(?:\.\d+)?", out)
            if m:
                return float(m.group(0)), f"cmd:{cmd.split()[0]}", None
        except Exception:
            return None, None, None
    for mod_name in _AGREEMENT_PROVIDERS:
        try:
            mod = __import__(mod_name)
        except Exception:
            continue
        summary_fn = getattr(mod, "scoring_agreement_summary", None)
        if callable(summary_fn):
            try:
                summary = summary_fn()
            except Exception:
                summary = None
            if summary and summary.get("agreement_pct") is not None:
                return float(summary["agreement_pct"]), mod_name, summary.get("measured_at")
        fn = getattr(mod, "scoring_agreement_pct", None)
        if callable(fn):
            try:
                val = fn()
            except Exception:
                val = None
            if val is not None:
                return float(val), mod_name, None
    return None, None, None


def scoring_agreement() -> dict:
    """Scoring agreement with the user's verdicts, as a number, tracked cycle to
    cycle. Delegates measurement to the golden-set eval harness (see the seam
    above) and reads the PREVIOUS cycle's number from the ledger for a trend.
    Graceful fallback when it has never been measured."""
    value, source, measured_at = _measure_agreement()
    previous = None
    try:
        prev = last_reviewed_detail()
        if prev:
            previous = prev.get("agreement")
    except Exception:
        previous = None
    return {
        "value": value,
        "measured_at": measured_at,
        "previous": previous,
        "measured": value is not None,
        "source": source,
        "note": None
        if value is not None
        else "not yet measured — run /jobs-eval to build a golden set and score it "
        "(review still works; the number fills in once you measure)",
    }


# ---------------------------------------------------------------------------
# The review assembly — everything the gate screen needs, computed here.
# ---------------------------------------------------------------------------


def build_review() -> dict:
    """Assemble the full learning-review payload (deterministic; no LLM).

    ``has_content`` decides whether the gate appears at all: only when there are
    verdicts to discuss or a proposal to make. When it appears it ALSO offers the
    filter-kill revision (~weekly) on the same screen.
    """
    if not table_ready():
        return {
            "ready": False,
            "has_content": False,
            "note": "learning_log table missing — run scripts/migrate.py to enable the "
            "learning cycle.",
        }

    garbage = undiscussed_garbage()
    decided = decided_since_cursor()
    liked = liked_titles()
    high = high_scored_titles()
    garbage_titles = [g["title"] for g in garbage if g["title"]]

    filter_props = propose_filter_words(
        garbage_titles, liked, high, existing=personal_filter_words()
    )

    try:
        from factors import load_factors, PENALTY, by_strength

        penalties = [f.text for f in by_strength(load_factors(), PENALTY)]
    except Exception:
        penalties = []
    factor_moves = propose_factor_moves(penalties, garbage_titles, liked, high)

    revision = sample_filter_kills() if revision_due() else []

    agreement = scoring_agreement()

    has_proposals = bool(filter_props["proposals"] or factor_moves)
    has_content = bool(decided or garbage or has_proposals or revision)

    ts = cursor_ts()
    return {
        "ready": True,
        "has_content": has_content,
        "cursor_at": str(ts) if ts is not None else None,
        "verdicts_since_last_review": decided,
        "garbage_count": len(garbage),
        "agreement": agreement,
        "proposals": {
            "filter_words": filter_props["proposals"],
            "filter_words_rejected": filter_props["rejected"],
            "factor_moves": factor_moves,
        },
        "revision": revision,
        "applied_recent": applied_log(limit=5),
    }


# ---------------------------------------------------------------------------
# CLI — the seam the /jobs-new gate calls. Deterministic, zero LLM spend.
# ---------------------------------------------------------------------------


def _cli(argv=None):
    import argparse

    p = argparse.ArgumentParser(description="Learning cycle — verdict-driven proposals (no LLM).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("review", help="Print the learning-review payload as JSON.")

    g = sub.add_parser("record-garbage", help="Record a 'garbage' verdict (a filter hole).")
    g.add_argument("--vacancy", required=True)
    g.add_argument("--title", default="")
    g.add_argument("--source", default="")
    g.add_argument("--score", default=None)

    a = sub.add_parser("apply", help="Apply an approved proposal (edits + logs).")
    a.add_argument(
        "--type",
        required=True,
        choices=["add_filter_word", "weaken_filter_word", "move_factor", "disable_board"],
    )
    a.add_argument("--word", help="word for add/weaken_filter_word")
    a.add_argument("--factor", help="penalty factor text for move_factor")
    a.add_argument("--keyword", help="filter keyword for move_factor")
    a.add_argument("--board", help="board id for disable_board")

    c = sub.add_parser("complete", help="Close the review cycle (advance rollover cursor).")
    c.add_argument("--agreement", default=None)
    c.add_argument("--applied", type=int, default=0)
    c.add_argument("--revision-shown", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "review":
        print(json.dumps(build_review(), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.cmd == "record-garbage":
        score = None
        if args.score not in (None, ""):
            try:
                score = int(args.score)
            except ValueError:
                score = None
        record_garbage(args.vacancy, args.title, args.source, score)
        print(f"recorded garbage verdict for {args.vacancy}")
        return 0
    if args.cmd == "apply":
        if args.type == "add_filter_word":
            print(json.dumps(apply_add_filter_word(args.word), ensure_ascii=False))
        elif args.type == "weaken_filter_word":
            print(json.dumps(apply_remove_filter_word(args.word), ensure_ascii=False))
        elif args.type == "move_factor":
            print(json.dumps(apply_factor_move(args.factor, args.keyword), ensure_ascii=False))
        elif args.type == "disable_board":
            print(json.dumps(apply_disable_board(args.board), ensure_ascii=False))
        return 0
    if args.cmd == "complete":
        agreement = args.agreement
        if agreement not in (None, ""):
            try:
                agreement = float(agreement)
            except ValueError:
                pass
        mark_reviewed(
            agreement=agreement, applied_count=args.applied, revision_shown=args.revision_shown
        )
        print("review cycle closed — verdicts marked discussed (cursor advanced)")
        return 0
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(_cli())

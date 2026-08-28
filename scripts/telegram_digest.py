#!/usr/bin/env python3
"""Telegram vacancy digest with 👍/👎 buttons.

Two modes:
  send  — ONE tiered morning message (split when Telegram's size limit forces
          it, and always before tier 2 and tier 3, so the tier-1 like/pass
          keyboard can only ever sit under the numbered entries it acts on;
          parts arrive in order): a counts header
          ("Night run: F fetched, S scored" for THIS run, then "Backlog now: D
          dropped, U still to score" counted in the database + "N deadlines this
          week"), tier 1 top matches with like/pass buttons (fresh rows scoring
          hot_vacancy_score+ plus strong roles at unreviewed companies), tier 2
          mid scores as one-liners, tier 3 every dropped vacancy as one line
          with its drop reason, tier 4 carried-over/rollover lines from the run
          state. Tiers 1–3 are claim-first per message part: tier 1–2 rows
          stamp digest_sent_at and tier 3 rows stamp digest_dropped_at just
          before the part that renders them is sent (recorded in the state
          file's pending_claim so even a SIGKILL cannot lose them); a failed
          part releases only its own claims. Tier 4 is gated by the last-digest
          timestamp in the state file. So a double-fire repeats nothing.
  poll  — long-poll getUpdates: button taps write vacancy.status
          (liked/passed) to the DB and the keyboard is redrawn with a ✅ mark.

Runs anywhere with Python + psycopg2. Typical setup: run `poll --loop` as a
long-lived daemon (e.g. a systemd service or supervisor process) so taps are
always captured, and run `send` from a scheduler / cron once a day. Only one
process per bot token may call getUpdates at a time.

Configuration (env vars, or the repo-root .env — auto-loaded, existing env wins):
  SUPABASE_DB_URL            — Postgres connection string (required)
  TELEGRAM_BOT_TOKEN         — bot token (required)
  TELEGRAM_CHAT_ID           — recipient chat id (required)
  DIGEST_STATE_FILE          — file storing the getUpdates offset
                               (default: ~/.jobsearch_digest_state.json)
"""

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_STATE_FILE = "~/.jobsearch_digest_state.json"

# Digest defaults come from config/defaults.toml ([digest]) when the settings
# loader is importable. This script also runs standalone on a bare host (no
# project tree), so fall back to neutral literals if settings is unavailable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unscored_pool  # noqa: E402 — the shared "waiting to be scored" definition

try:
    import settings as _settings  # noqa: E402

    _DIGEST = _settings.digest()
except Exception:
    _DIGEST = {
        "hot_vacancy_score": 55,
        "deadline_soon_days": 7,
        "default_limit": 5,
        "mid_min_score": 40,
        "dropped_max_lines": 25,
        "summary_fallback_chars": 600,
        "summary_max_chars": 1500,
        "message_max_chars": 4000,
    }

SUMMARY_FALLBACK_CHARS = _DIGEST["summary_fallback_chars"]
SUMMARY_MAX_CHARS = _DIGEST["summary_max_chars"]  # guard the Telegram message limit
MESSAGE_MAX_CHARS = _DIGEST["message_max_chars"]
CALLBACK_PREFIX = "v"
ACTION_TO_STATUS = {"l": "liked", "p": "passed", "a": "applied"}

# Product language: the digest speaks the ONE language chosen in the
# profile's ## OUTPUT_LANGUAGE. Every user-facing string routes through _t(),
# which reads the resolved language and the bundled tables in scripts/i18n.py.
# product_language lives next to this script; a catastrophic import failure
# degrades to the raw key rather than crashing the digest.
try:
    import product_language as _pl

    def _t(key, **fmt):
        return _pl.t(key, **fmt)
except Exception:  # pragma: no cover — same dir, effectively always importable

    def _t(key, **fmt):
        return key


def _status_label(status):
    """Localized button/label text for a recorded status (liked/passed/applied)."""
    return _t(f"digest_status_{status}")


SELECT_FRESH_SQL = """
SELECT v.id, v.title, c.canonical_name AS org, v.llm_score, v.llm_summary,
       v.full_description, v.snippet, v.locations, v.compensation
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE v.status = 'unseen'
  AND c.status = 'active'
  AND v.digest_sent_at IS NULL
  AND v.llm_score IS NOT NULL
  AND v.llm_score >= %s
  AND (v.llm_summary IS NOT NULL OR length(coalesce(v.full_description, '')) >= 200)
ORDER BY v.llm_score DESC, v.created_at DESC
LIMIT %s
"""

# Strong vacancies at companies you haven't reviewed yet (status=candidate).
# They join tier 1 with like/pass buttons, so a deadline isn't missed while the
# company sits in the review queue; digest_sent_at keeps them to one send.
HOT_VACANCY_SCORE = int(os.environ.get("DIGEST_HOT_SCORE", _DIGEST["hot_vacancy_score"]))
DEADLINE_SOON_DAYS = int(os.environ.get("DIGEST_DEADLINE_SOON_DAYS", _DIGEST["deadline_soon_days"]))
MID_MIN_SCORE = int(os.environ.get("DIGEST_MID_MIN_SCORE", _DIGEST["mid_min_score"]))
DROPPED_MAX_LINES = int(_DIGEST["dropped_max_lines"])
SELECT_CANDIDATE_HOT_SQL = """
SELECT v.id, v.title, c.canonical_name AS org, v.llm_score,
       v.locations, v.deadline
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE c.status = 'candidate'
  AND v.digest_sent_at IS NULL
  AND v.llm_score IS NOT NULL
  AND v.llm_score >= %s
  AND v.status NOT IN ('passed', 'skipped', 'archived')
ORDER BY v.llm_score DESC, v.created_at DESC
LIMIT %s
"""

# Tier 2: mid scores — one line each (title, company, score, link), stamped
# with digest_sent_at like tier 1 so a double-fire repeats nothing.
SELECT_MID_SQL = """
SELECT v.id, v.title, c.canonical_name AS org, v.llm_score, v.locations
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE v.status = 'unseen'
  AND c.status = 'active'
  AND v.digest_sent_at IS NULL
  AND v.llm_score IS NOT NULL
  AND v.llm_score >= %s
  AND v.llm_score < %s
ORDER BY v.llm_score DESC, v.created_at DESC
"""

# Tier 3: dropped vacancies (migration 0025) not shown in a digest yet.
# Claimed like tiers 1–2: the fetched (capped) rows stamp digest_dropped_at
# (migration 0026) before their message part is sent, released on failure.
# Rows beyond the cap stay unstamped and surface next morning. A timestamp
# cutoff on first_seen cannot do this job — first_seen is a DATE, so rows
# dropped later the same day would sit below the cutoff forever. And
# digest_sent_at is not reused: a dropped row whose exclusion is later cleared
# and scored must still be able to appear in tiers 1–2.
SELECT_DROPPED_SQL = """
SELECT v.id, v.title, c.canonical_name AS org, v.locations,
       v.scoring_excluded_reason
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE v.scoring_excluded_reason IS NOT NULL
  AND v.digest_dropped_at IS NULL
ORDER BY v.first_seen DESC, v.title
LIMIT %s
"""

COUNT_DROPPED_SQL = """
SELECT count(*) AS n
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE v.scoring_excluded_reason IS NOT NULL
  AND v.digest_dropped_at IS NULL
"""

# Header fallback when there is no fresh run state: counts over everything
# first seen since the last digest. The dropped column here shares first_seen's
# date-granularity blind spot, so gather_counts overrides it with the tier-3
# not-yet-shown total — the header then always matches what tier 3 reports.
COUNT_SINCE_SQL = """
SELECT count(*) AS fetched,
       sum(CASE WHEN v.llm_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
       sum(CASE WHEN v.scoring_excluded_reason IS NOT NULL THEN 1 ELSE 0 END) AS dropped,
       sum(CASE WHEN v.llm_score IS NULL AND v.scoring_excluded_reason IS NULL
                THEN 1 ELSE 0 END) AS unscored
FROM vacancy v
WHERE v.first_seen > %s
"""

# Protected high-fit roles (status='expiring') that haven't been alerted yet.
# These are the scarce decisions latency protection exists to save, so they get
# a loud, separate message — not the score-ranked daily batch. Fires once per
# role (expiring_alerted_at gate).
SELECT_EXPIRING_SQL = """
SELECT v.id, v.title, c.canonical_name AS org, v.llm_score, v.llm_summary,
       v.full_description, v.snippet, v.locations, v.compensation,
       v.deadline, v.last_seen
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE v.status = 'expiring'
  AND v.expiring_alerted_at IS NULL
ORDER BY v.llm_score DESC NULLS LAST, v.last_seen ASC
"""


# --- env / config -----------------------------------------------------------


def load_dotenv_fallback():
    """Load the repo-root ``.env`` so a host without a shell profile still runs.

    Reuses ``db_backend.load_dotenv`` so the digest reads exactly the same file
    (repo root, existing env wins) as the rest of the pipeline. On a bare host
    without the project tree, fall back to a local parse of a ``.env`` at the
    repo root or next to this script.
    """
    try:
        import db_backend

        # Importing db_backend already ran load_dotenv() once; this explicit
        # call is a harmless no-op then (setdefault never overwrites existing
        # vars) and only matters if db_backend was imported before the .env
        # existed or with loading disabled. Kept for clarity: this script's
        # config contract is "repo-root .env is loaded by the time get_config
        # runs", independent of import order.
        db_backend.load_dotenv()
        return
    except Exception:
        pass

    here = Path(__file__).resolve().parent
    for env_path in (here.parent / ".env", here / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


def get_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    db_url = os.environ.get("SUPABASE_DB_URL")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set in the environment")
    if not db_url:
        # No Postgres URL: fall back to the project's SQLite backend (the
        # simple-mode install). A bare host without the project tree still
        # needs SUPABASE_DB_URL.
        try:
            import db_backend

            assert db_backend.IS_SQLITE
        except Exception:
            sys.exit("SUPABASE_DB_URL is not set in the environment")
    if not chat_id:
        sys.exit("TELEGRAM_CHAT_ID is not set in the environment")
    return token, db_url, chat_id


# --- Telegram Bot API (urllib, no dependencies) -----------------------------


def tg_call(token, method, payload, timeout=15, retries=2):
    """Call the Bot API. Short timeout + retries: one stuck call must not
    block the callback queue (a button ack lives for ~15 seconds)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                raise RuntimeError(f"{method} HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise RuntimeError(f"{method} network error: {e}") from e
        if not body.get("ok"):
            raise RuntimeError(f"{method} failed: {body.get('description')}")
        return body["result"]
    raise RuntimeError(f"{method} network error: {last_err}")


# --- message building --------------------------------------------------------


def vacancy_url(row):
    for loc in row.get("locations") or []:
        if isinstance(loc, dict) and loc.get("url"):
            return loc["url"]
    return None


def vacancy_location(row):
    parts = []
    for loc in row.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        place = loc.get("city") or loc.get("country") or loc.get("region")
        mode = loc.get("work_mode")
        bits = [b for b in (place, mode) if b]
        if bits:
            parts.append(", ".join(bits))
        comp = loc.get("compensation")
        if comp and not row.get("compensation"):
            row["compensation"] = comp
        if parts:
            break  # the first location is enough for the digest
    return parts[0] if parts else _t("digest_loc_unspecified")


def deadline_soon_label(deadline_val):
    """'deadline DD.MM' if the deadline is within DEADLINE_SOON_DAYS days, else ''."""
    if not deadline_val:
        return ""
    import datetime as _dt

    if isinstance(deadline_val, _dt.date):
        dl = deadline_val
    else:
        try:
            dl = _dt.date.fromisoformat(str(deadline_val)[:10])
        except (ValueError, TypeError):
            return ""
    days_left = (dl - _dt.date.today()).days
    if 0 <= days_left <= DEADLINE_SOON_DAYS:
        return _t("digest_deadline", date=f"{dl.day:02d}.{dl.month:02d}")
    return ""


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def vacancy_summary(row):
    if row.get("llm_summary"):
        return _truncate(row["llm_summary"].strip(), SUMMARY_MAX_CHARS)
    text = (row.get("full_description") or row.get("snippet") or "").strip()
    return _truncate(" ".join(text.split()), SUMMARY_FALLBACK_CHARS)


# How much of a top match's summary survives in its tier-1 entry: enough to
# decide like/pass, small enough that five entries still fit one message.
TOP_SUMMARY_CHARS = 200


def _link_suffix(row):
    url = vacancy_url(row)
    if not url:
        return ""
    return f' — <a href="{html.escape(url, quote=True)}">{_t("digest_open_short")}</a>'


def _org_title(row):
    """HTML-escaped (org, title) pair every line builder starts from."""
    return html.escape(row.get("org") or ""), html.escape(row.get("title") or "")


def build_top_line(row, index):
    """Tier-1 entry: numbered bold line (+ a short summary line) with a link.
    The number matches the entry's 👍/👎 keyboard row."""
    org, title = _org_title(row)
    bits = [f"{index}. <b>{org}</b> — {title}"]
    if row.get("llm_score") is not None:
        bits.append(f"🎯 {row['llm_score']}")
    dl = deadline_soon_label(row.get("deadline"))
    if dl:
        bits.append(f"⏰ {html.escape(dl)}")
    line = " · ".join(bits) + _link_suffix(row)
    summary = (row.get("llm_summary") or "").strip()
    if summary:
        line += "\n" + html.escape(_truncate(summary, TOP_SUMMARY_CHARS))
    return line


def build_mid_line(row):
    """Tier-2 entry: one line — title, company, score, link (R7)."""
    org, title = _org_title(row)
    line = f"• {org} — {title}"
    if row.get("llm_score") is not None:
        line += f" · {row['llm_score']}"
    return line + _link_suffix(row)


def build_dropped_line(row):
    """Tier-3 entry: one line — title, company, the drop reason, link (AE2)."""
    org, title = _org_title(row)
    reason = html.escape(row.get("scoring_excluded_reason") or "")
    return f"• {title} — {org} — {_t('digest_dropped_prefix')} {reason}" + _link_suffix(row)


# --- digest state file (offset + last-digest timestamp) ----------------------


def read_state_file(path):
    """The digest's own state file as a dict ({} when missing/corrupt)."""
    try:
        return json.loads(Path(path).expanduser().read_text())
    except Exception:
        return {}


def update_state_file(path, **fields):
    """Merge ``fields`` into the state file — the poller's offset and the
    sender's last_digest_at/pending_claim share the file. The read-merge-write
    runs under an exclusive flock so the long-running poller and the nightly
    sender cannot clobber each other's fields (best-effort on platforms
    without fcntl)."""
    p = Path(path).expanduser()
    try:
        import fcntl

        lock_path = p.with_suffix(p.suffix + ".lock")
        with open(lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            state = read_state_file(p)
            state.update(fields)
            p.write_text(json.dumps(state))
    except ImportError:
        state = read_state_file(p)
        state.update(fields)
        p.write_text(json.dumps(state))


# --- run state (written by run_daily.py / the U4 nightly wrapper) -------------
#
# Field contract the digest reads from vacancies/run_state.json:
#   * top-level "no_progress": true — set by the nightly wrapper when the
#     scoring session exited normally but saved no scores (AE7). N in the
#     header line comes from stages[vacancy_scoring].target_ids (fallback:
#     carried_over).
#   * optional top-level "counts" {"new_vacancies": F, "scored": S}.
#     Missing pieces degrade to fetch_stats.json / stage fields.
#   * stages[vacancy_scoring|company_scoring].carried_over,
#     stages[learning_review].rolled_over, stages[verdicts].pending_verdicts
#     — the tier-4 lines.
# The header's "dropped" and "still to score" are NOT read from here: they are
# counted in the database, so a reader can check them against what tier 3 lists
# and against the dashboard.


def run_state_path():
    override = os.environ.get("DIGEST_RUN_STATE_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "vacancies" / "run_state.json"


def load_run_state(last_digest_iso=None):
    """Parsed run_state.json, or None when missing, corrupt, or already
    digested (its updated_at predates the last digest — a double-fire must not
    repeat the night's header and tier-4 lines)."""
    try:
        state = json.loads(run_state_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if last_digest_iso:
        stamp = str(state.get("updated_at") or state.get("created_at") or "")
        if stamp <= str(last_digest_iso):
            return None
    return state


def _run_stage(state, name):
    for s in state.get("stages", []) or []:
        if isinstance(s, dict) and s.get("name") == name:
            return s
    return {}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fetch_stats_total_new():
    try:
        stats = json.loads((run_state_path().parent / "fetch_stats.json").read_text())
        return _int(stats.get("total_new"))
    except Exception:
        return 0


def fetch_unscored_pool(conn):
    """Roles waiting to be scored right now, by company status. ONE definition,
    shared with the filter stage's note — see scripts/unscored_pool.py."""
    with _dict_cursor(conn) as cur:
        return unscored_pool.counts(cur)


def gather_counts(conn, run_state, since, dropped_total=None):
    """Header numbers (R10), each one checkable.

    Two denominators, said in that order and labelled in the header string:
      * ``fetched`` / ``scored`` — THIS run (from the run state, else from
        rows first seen since the last digest);
      * ``dropped`` / ``unscored`` — the backlog as it stands NOW. ``dropped``
        is ``dropped_total``, the count of not-yet-shown dropped rows, so it
        equals what tier 3 claims; ``unscored`` is the live pool of roles
        awaiting a score — NOT the carried-over batch, which is a subset of it
        and has its own tier-4 line.
    """
    pool = fetch_unscored_pool(conn)
    unscored, parked = pool["active"], pool["candidate"]
    if run_state is None:
        counts = fetch_counts_since(conn, since)
        if dropped_total is not None:
            counts["dropped"] = dropped_total
        counts["unscored"] = unscored
        counts["parked"] = parked
        counts["targets"] = 0
        return counts
    c = run_state.get("counts") or {}
    vac = _run_stage(run_state, "vacancy_scoring")
    target_ids = vac.get("target_ids")
    carried = _int(vac.get("carried_over"))
    targets = len(target_ids) if isinstance(target_ids, list) else carried
    fetched = c.get("new_vacancies")
    if fetched is None:
        fetched = _fetch_stats_total_new()
    if run_state.get("no_progress"):
        scored = 0
    elif c.get("scored") is not None:
        scored = c["scored"]
    else:
        scored = max(targets - carried, 0)
    return {
        "fetched": _int(fetched),
        "scored": _int(scored),
        "dropped": _int(dropped_total),
        "unscored": unscored,
        "parked": parked,
        "targets": targets,
    }


def build_header_lines(counts, deadlines_soon, run_state):
    """Counts line first — a short message must read "quiet night", never
    "broken night" (R10) — then the parked-backlog line, the AE7 no-progress
    line, then deadlines."""
    lines = [
        _t(
            "digest_run_header",
            fetched=counts["fetched"],
            scored=counts["scored"],
            dropped=counts["dropped"],
            unscored=counts["unscored"],
        )
    ]
    # A big parked backlog must never hide behind a small "waiting" figure:
    # roles at unapproved companies are unscored and invisible everywhere else.
    if counts.get("parked"):
        lines.append(_t("digest_waiting_parked", n=counts["parked"]))
    if run_state and run_state.get("no_progress"):
        lines.append(_t("digest_no_progress", n=counts["targets"]))
    if deadlines_soon:
        lines.append(_t("digest_deadlines_week", n=deadlines_soon))
    return lines


def build_tail_lines(run_state):
    """Tier 4: carried-over / rolled-over / pending-verdict one-liners."""
    if not run_state:
        return []
    lines = []
    what = []
    n = _int(_run_stage(run_state, "vacancy_scoring").get("carried_over"))
    if n:
        what.append(_t("digest_carried_roles", n=n))
    m = _int(_run_stage(run_state, "company_scoring").get("carried_over"))
    if m:
        what.append(_t("digest_carried_companies", n=m))
    if what:
        lines.append(_t("digest_carried_over", what=", ".join(what)))
    r = _int(_run_stage(run_state, "learning_review").get("rolled_over"))
    if r:
        lines.append(_t("digest_rolled_over", n=r))
    p = _int(_run_stage(run_state, "verdicts").get("pending_verdicts"))
    if p:
        lines.append(_t("digest_pending_verdicts", n=p))
    return lines


#: Block sentinel: the next block starts a NEW message part. Not rendered.
PART_BREAK = "\x00PART_BREAK\x00"


def assemble_digest(header_lines, top_rows, mid_rows, dropped_rows, tail_lines, dropped_total=None):
    """All blocks of the morning message, in tier order (R6). Blocks that
    render a database row are ``(text, row, tier)`` tuples so the sender can
    claim, release and keyboard exactly the rows of each message part;
    text-only blocks stay plain strings. ``dropped_rows`` arrives pre-capped by
    the fetch; ``dropped_total`` (default: its length) drives the "+N more"
    tail."""
    if dropped_total is None:
        dropped_total = len(dropped_rows)
    blocks = list(header_lines)
    if top_rows:
        blocks.append("")
        blocks.append(_t("digest_tier_top"))
        blocks.extend((build_top_line(r, i), r, "top") for i, r in enumerate(top_rows, 1))
    if mid_rows:
        # Tier 2 opens a NEW message for the same reason tier 3 does: the
        # tier-1 keyboard sits at the bottom of its message, and a message that
        # ends on "Mid scores" lines puts the buttons under rows they do not
        # act on. Tier 1 therefore always ends on its own numbered entries.
        blocks.append(PART_BREAK)
        blocks.append(_t("digest_tier_mid"))
        blocks.extend((build_mid_line(r), r, "mid") for r in mid_rows)
    if dropped_rows:
        blocks.append(PART_BREAK)
        blocks.append(_t("digest_tier_dropped"))
        shown = dropped_rows[:DROPPED_MAX_LINES]
        blocks.extend((build_dropped_line(r), r, "dropped") for r in shown)
        if dropped_total > len(shown):
            blocks.append(_t("digest_dropped_more", n=dropped_total - len(shown)))
    if tail_lines:
        blocks.append("")
        blocks.extend(tail_lines)
    if not (top_rows or mid_rows or dropped_rows or tail_lines):
        blocks.append("")
        blocks.append(_t("digest_quiet"))
    return blocks


def split_message_parts(blocks, limit=None):
    """Pack blocks into message parts of at most ``limit`` chars, splitting
    only at block boundaries so order and lines survive intact (R6). A block is
    a plain string, the ``PART_BREAK`` sentinel (closes the current part and
    renders nothing), or an ``(text, row, tier)`` tuple (assemble_digest); each
    part comes back as ``{"text": str, "rows": [(row, tier), ...]}`` with the
    rows rendered inside that part."""
    limit = limit or MESSAGE_MAX_CHARS
    parts = []
    cur_text, cur_rows = "", []
    for block in blocks:
        if block == PART_BREAK:
            if cur_text.strip():
                parts.append({"text": cur_text, "rows": cur_rows})
                cur_text, cur_rows = "", []
            continue
        text, row, tier = block if isinstance(block, tuple) else (block, None, None)
        if len(text) > limit:
            text = _truncate(text, limit - 1)
        candidate = text if not cur_text else cur_text + "\n" + text
        if len(candidate) > limit:
            parts.append({"text": cur_text, "rows": cur_rows})
            cur_text, cur_rows = text, []
        else:
            cur_text = candidate
        if row is not None:
            cur_rows.append((row, tier))
    if cur_text.strip() or not parts:
        parts.append({"text": cur_text, "rows": cur_rows})
    return parts


def split_message(blocks, limit=None):
    """The packed part texts only — the historical string-in/string-out view
    of ``split_message_parts``."""
    return [p["text"] for p in split_message_parts(blocks, limit)]


def build_digest_keyboard(rows, numbers=None):
    """One 👍/👎 keyboard row per tier-1 entry, numbered like the entries.
    ``numbers`` carries the global entry numbers when a split leaves only a
    slice of tier 1 on this part — buttons must show the same numbers as the
    rendered lines."""
    if numbers is None:
        numbers = range(1, len(rows) + 1)
    return {
        "inline_keyboard": [
            [
                {"text": f"👍 {i}", "callback_data": f"{CALLBACK_PREFIX}:{row['id']}:l"},
                {"text": f"👎 {i}", "callback_data": f"{CALLBACK_PREFIX}:{row['id']}:p"},
            ]
            for i, row in zip(numbers, rows)
        ]
    }


def rebuild_markup(existing, vac_id, chosen):
    """The message's keyboard with ✅ on the chosen action of ``vac_id`` only.

    The tiered digest puts several vacancies' button rows on one message, so a
    tap must redraw from the keyboard the message already carries — rebuilding
    a fresh single-row keyboard would wipe every other vacancy's buttons."""
    rows = (existing or {}).get("inline_keyboard")
    if not rows:
        return build_keyboard(vac_id, chosen)
    marked = []
    for row in rows:
        new_row = []
        for btn in row:
            text = btn.get("text", "")
            if text.startswith("✅ "):
                text = text[len("✅ ") :]
            parsed = parse_callback(btn.get("callback_data"))
            if parsed and parsed[0] == str(vac_id) and parsed[1] == chosen:
                text = "✅ " + text
            new_row.append({"text": text, "callback_data": btn.get("callback_data")})
        marked.append(new_row)
    return {"inline_keyboard": marked}


def _deadline_or_last_seen_line(row):
    """A short 'why this is expiring' line for the alert."""
    dl = deadline_soon_label(row.get("deadline"))
    if dl:
        return f"⏳ {html.escape(dl)}"
    ls = row.get("last_seen")
    if ls:
        return _t("digest_last_seen", date=html.escape(str(ls)[:10]))
    return ""


def build_expiring_message(row):
    """Loud single-role alert for a protected role about to disappear."""
    org, title = _org_title(row)
    lines = [
        _t("digest_expiring_header"),
        f"<b>{org} — {title}</b>",
    ]
    meta = [f"📍 {html.escape(vacancy_location(row))}"]
    if row.get("llm_score") is not None:
        meta.append(f"🎯 {row['llm_score']}/100")
    why = _deadline_or_last_seen_line(row)
    if why:
        meta.append(why)
    lines.append(" · ".join(meta))

    summary = vacancy_summary(row)
    if summary:
        lines.append("")
        lines.append(html.escape(summary))

    url = vacancy_url(row)
    if url:
        lines.append("")
        lines.append(f'<a href="{html.escape(url, quote=True)}">{_t("digest_open")}</a>')
    text = "\n".join(lines)
    if len(text) > MESSAGE_MAX_CHARS:
        overflow = len(text) - MESSAGE_MAX_CHARS
        summary = html.escape(vacancy_summary(row))
        keep = max(200, len(summary) - overflow - 1)
        lines = [l if l != summary else _truncate(summary, keep) for l in lines]
        text = "\n".join(lines)
    return text


def build_expiring_keyboard(vac_id):
    """👍 / 👎 / «уже подал» for an expiring-role alert. Reuses the v:<id>:<a>
    callback format so the existing poll handler records the decision."""
    return {
        "inline_keyboard": [
            [
                {"text": _status_label("liked"), "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:l"},
                {"text": _status_label("passed"), "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:p"},
                {
                    "text": _status_label("applied"),
                    "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:a",
                },
            ]
        ]
    }


def build_keyboard(vac_id, chosen=None):
    """👍/👎 buttons; the chosen one gets a ✅ (tapping again flips the choice)."""
    like = _status_label("liked")
    pas = _status_label("passed")
    if chosen == "liked":
        like = "✅ " + like
    elif chosen == "passed":
        pas = "✅ " + pas
    return {
        "inline_keyboard": [
            [
                {"text": like, "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:l"},
                {"text": pas, "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:p"},
            ]
        ]
    }


def parse_callback(data):
    """'v:<uuid>:l' -> (uuid, 'liked') | None when the format is foreign."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    status = ACTION_TO_STATUS.get(parts[2])
    if not status or not parts[1]:
        return None
    return parts[1], status


# --- database ----------------------------------------------------------------


def db_connect(db_url):
    # Documented exception to the DAL's "autocommit OFF — callers commit" rule
    # (database_supabase.py): the digest poller opens its OWN short-lived
    # psycopg2 connection, separate from the shared DAL singleton, and writes
    # one button-response status at a time. autocommit=True is intentional here
    # so each poll persists immediately; it does not touch the DAL connection
    # and so cannot cause a silent rollback of DAL-staged writes.
    #
    # Without a db_url (simple mode) the shared SQLite backend connection is
    # used instead; the mark_* helpers commit explicitly, which psycopg2
    # treats as a no-op under autocommit, so both paths persist every stamp.
    if not db_url:
        import db_backend

        return db_backend.get_conn()
    import psycopg2

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    return conn


def _dict_cursor(conn):
    """A RealDictCursor on either backend (psycopg2 or the SQLite wrapper)."""
    try:
        from psycopg2.extras import RealDictCursor
    except ImportError:
        from db_backend import RealDictCursor
    return conn.cursor(cursor_factory=RealDictCursor)


def fetch_fresh(conn, limit, min_score):
    with _dict_cursor(conn) as cur:
        cur.execute(SELECT_FRESH_SQL, (min_score, limit))
        return [dict(r) for r in cur.fetchall()]


def fetch_candidate_hot(conn, limit=10, min_score=HOT_VACANCY_SCORE):
    with _dict_cursor(conn) as cur:
        cur.execute(SELECT_CANDIDATE_HOT_SQL, (min_score, limit))
        return [dict(r) for r in cur.fetchall()]


def fetch_mid(conn, min_score, max_score):
    with _dict_cursor(conn) as cur:
        cur.execute(SELECT_MID_SQL, (min_score, max_score))
        return [dict(r) for r in cur.fetchall()]


def fetch_dropped(conn):
    """(rows capped at DROPPED_MAX_LINES, total not-yet-shown count)."""
    with _dict_cursor(conn) as cur:
        cur.execute(SELECT_DROPPED_SQL, (DROPPED_MAX_LINES,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(COUNT_DROPPED_SQL)
        total = int(dict(cur.fetchone() or {}).get("n") or 0)
    return rows, total


def fetch_counts_since(conn, since):
    with _dict_cursor(conn) as cur:
        cur.execute(COUNT_SINCE_SQL, (since,))
        row = dict(cur.fetchone() or {})
    return {k: int(row.get(k) or 0) for k in ("fetched", "scored", "dropped", "unscored")}


def fetch_expiring(conn):
    with _dict_cursor(conn) as cur:
        cur.execute(SELECT_EXPIRING_SQL)
        return [dict(r) for r in cur.fetchall()]


# The mark/unmark helpers commit explicitly: a no-op on the autocommit psycopg2
# connection, required on the shared SQLite backend connection.


def mark_alerted(conn, vac_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancy SET expiring_alerted_at = now() WHERE id = %s",
            (vac_id,),
        )
    conn.commit()


def unmark_alerted(conn, vac_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancy SET expiring_alerted_at = NULL WHERE id = %s",
            (vac_id,),
        )
    conn.commit()


def _stamp_many(conn, set_clause, ids):
    """One UPDATE for a whole claim/release batch (no per-row round trips)."""
    if not ids:
        return
    ph = ", ".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(f"UPDATE vacancy SET {set_clause} WHERE id IN ({ph})", list(ids))
    conn.commit()


def mark_sent_many(conn, vac_ids):
    _stamp_many(conn, "digest_sent_at = now()", vac_ids)


def unmark_sent_many(conn, vac_ids):
    _stamp_many(conn, "digest_sent_at = NULL", vac_ids)


def mark_alerted_many(conn, vac_ids):
    _stamp_many(conn, "expiring_alerted_at = now()", vac_ids)


def unmark_alerted_many(conn, vac_ids):
    _stamp_many(conn, "expiring_alerted_at = NULL", vac_ids)


def mark_dropped_many(conn, vac_ids):
    _stamp_many(conn, "digest_dropped_at = now()", vac_ids)


def unmark_dropped_many(conn, vac_ids):
    _stamp_many(conn, "digest_dropped_at = NULL", vac_ids)


def set_status(conn, vac_id, status):
    """Returns the title, or None when the vacancy isn't found."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE vacancy SET status = %s, status_updated_at = now()
               WHERE id = %s RETURNING title""",
            (status, vac_id),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


# --- send mode ----------------------------------------------------------------


def send_expiring_alerts(conn, token, chat_id, expiring_rows, dry_run=False):
    """Send one loud alert per freshly-expiring protected role. Returns the
    number sent. Each is claimed (mark_alerted) before sending so a parallel run
    or a re-run never double-alerts; a send failure releases it for next time."""
    if not expiring_rows:
        return 0
    if dry_run:
        for row in expiring_rows:
            print("--- expiring alert ---")
            print(build_expiring_message(row))
        print(f"[dry-run] {len(expiring_rows)} expiring alert(s), nothing sent", flush=True)
        return 0
    sent = 0
    for row in expiring_rows:
        mark_alerted(conn, row["id"])  # claim-first
        try:
            tg_call(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": build_expiring_message(row),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": build_expiring_keyboard(row["id"]),
                },
            )
            sent += 1
            time.sleep(0.5)
        except Exception as e:
            unmark_alerted(conn, row["id"])  # release for the next run
            print(f"ERROR expiring alert '{row.get('title')}': {e}", file=sys.stderr, flush=True)
    print(f"Sent {sent}/{len(expiring_rows)} expiring alert(s) to chat {chat_id}", flush=True)
    return sent


def cmd_alert(args):
    """Standalone: send only the loud expiring-role alerts."""
    token, db_url, chat_id = get_config()
    conn = db_connect(db_url)
    expiring_rows = fetch_expiring(conn)
    if not expiring_rows:
        print("No freshly-expiring roles to alert.", flush=True)
        return
    send_expiring_alerts(conn, token, chat_id, expiring_rows, dry_run=args.dry_run)


def _since_param(db_url, since_iso):
    """The last-digest cutoff typed for the backend: a datetime for psycopg2
    (so Postgres compares first_seen::timestamp against the full timestamp,
    not a truncated date), the ISO string for SQLite (text comparison)."""
    if not db_url:
        return since_iso
    try:
        return datetime.fromisoformat(since_iso)
    except (ValueError, TypeError):
        return since_iso


def release_pending_claim(conn, state_path):
    """Release the claims a killed run left behind. cmd_send records each
    part's ids in the state file just before stamping them; a SIGKILL between
    stamp and send would otherwise lose those rows forever (the in-process
    release never runs). Worst case — killed after a delivered send but before
    the state update — the release repeats one part next morning: a duplicate,
    never a loss."""
    pending = read_state_file(state_path).get("pending_claim")
    if not pending:
        return
    unmark_sent_many(conn, pending.get("sent") or [])
    unmark_dropped_many(conn, pending.get("dropped") or [])
    unmark_alerted_many(conn, pending.get("alerted") or [])
    update_state_file(state_path, pending_claim=None)


def cmd_send(args):
    token, db_url, chat_id = get_config()
    conn = db_connect(db_url)
    state_path = os.environ.get("DIGEST_STATE_FILE", DEFAULT_STATE_FILE)
    last_digest = read_state_file(state_path).get("last_digest_at")
    since_iso = last_digest or (date.today() - timedelta(days=1)).isoformat()
    since = _since_param(db_url, since_iso)
    run_state = load_run_state(last_digest)

    # A previous run may have died between claiming a part and sending it —
    # release those claims before fetching so the rows join THIS digest.
    # (Skipped on dry-run, which must not write anywhere.)
    if not args.dry_run:
        release_pending_claim(conn, state_path)

    # Tier 1: fresh top matches + strong roles at unreviewed companies (KTD5).
    top_rows = fetch_fresh(conn, args.limit, HOT_VACANCY_SCORE)
    top_rows += fetch_candidate_hot(conn)
    mid_floor = args.min_score if args.min_score is not None else MID_MIN_SCORE
    mid_rows = fetch_mid(conn, mid_floor, HOT_VACANCY_SCORE)
    dropped_rows, dropped_total = fetch_dropped(conn)
    # Expiring deadlines fold into one header line instead of loud alerts.
    expiring_rows = fetch_expiring(conn)

    counts = gather_counts(conn, run_state, since, dropped_total)
    header = build_header_lines(counts, len(expiring_rows), run_state)
    tail = build_tail_lines(run_state)
    blocks = assemble_digest(header, top_rows, mid_rows, dropped_rows, tail, dropped_total)
    parts = split_message_parts(blocks)

    if args.dry_run:
        for i, part in enumerate(parts, 1):
            print(f"--- message {i}/{len(parts)} ---")
            print(part["text"])
        print(
            f"\n[dry-run] {len(parts)} message(s): {len(top_rows)} top, {len(mid_rows)} mid, "
            f"{dropped_total} dropped, {len(expiring_rows)} deadline(s) — nothing sent",
            flush=True,
        )
        return

    # Claim-first, one message part at a time: stamp exactly the rows a part
    # renders just before sending it, with the ids journalled to the state
    # file first (pending_claim) so even a SIGKILL between stamp and send is
    # released by the next run. A failed part releases only its own claims —
    # earlier parts were delivered and stay stamped, later parts were never
    # claimed (failure delays, never loses). Expiring deadlines ride the
    # header's part (part 0). Tier-1 numbering is global across parts, and
    # each part's keyboard carries the numbers of the entries it renders.
    numbers = {row["id"]: i for i, row in enumerate(top_rows, 1)}
    for i, part in enumerate(parts):
        part_top = [row for row, tier in part["rows"] if tier == "top"]
        claim_sent = [row["id"] for row, tier in part["rows"] if tier in ("top", "mid")]
        claim_dropped = [row["id"] for row, tier in part["rows"] if tier == "dropped"]
        claim_alerted = [row["id"] for row in expiring_rows] if i == 0 else []
        update_state_file(
            state_path,
            pending_claim={
                "sent": claim_sent,
                "dropped": claim_dropped,
                "alerted": claim_alerted,
            },
        )
        mark_sent_many(conn, claim_sent)
        mark_dropped_many(conn, claim_dropped)
        mark_alerted_many(conn, claim_alerted)
        payload = {
            "chat_id": chat_id,
            "text": part["text"],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if part_top:
            payload["reply_markup"] = build_digest_keyboard(
                part_top, [numbers[row["id"]] for row in part_top]
            )
        try:
            tg_call(token, "sendMessage", payload)
        except Exception as e:
            unmark_sent_many(conn, claim_sent)
            unmark_dropped_many(conn, claim_dropped)
            unmark_alerted_many(conn, claim_alerted)
            update_state_file(state_path, pending_claim=None)
            print(
                f"ERROR digest send (part {i + 1}/{len(parts)}): {e}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)
        time.sleep(0.5)  # respect the rate limit
    update_state_file(
        state_path,
        pending_claim=None,
        last_digest_at=datetime.now().isoformat(timespec="seconds"),
    )
    print(
        f"Digest sent to chat {chat_id}: {len(parts)} message(s) — {len(top_rows)} top, "
        f"{len(mid_rows)} mid, {dropped_total} dropped, "
        f"{len(expiring_rows)} deadline(s).",
        flush=True,
    )


# --- poll mode ----------------------------------------------------------------


def load_offset(state_file):
    return read_state_file(state_file).get("offset")


def save_offset(state_file, offset):
    update_state_file(state_file, offset=offset)


def handle_callback(conn, token, cb, allowed_user=None):
    sender = str((cb.get("from") or {}).get("id", ""))
    if allowed_user and sender != str(allowed_user):
        tg_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        print(f"Tap from a foreign user {sender} — ignored", flush=True)
        return
    parsed = parse_callback(cb.get("data"))

    # 1. Kill the spinner immediately — the ack expires in ~15 seconds.
    ack = {"callback_query_id": cb["id"]}
    if parsed:
        ack["text"] = _t("digest_recorded", label=_status_label(parsed[1]))
    try:
        tg_call(token, "answerCallbackQuery", ack)
    except RuntimeError as e:
        print(f"WARN answerCallbackQuery: {e}", file=sys.stderr, flush=True)
    if not parsed:
        return

    # 2. Write the decision to the DB.
    vac_id, status = parsed
    msg = cb.get("message") or {}
    try:
        title = set_status(conn, vac_id, status)
    except Exception as e:
        print(f"ERROR DB update {vac_id}: {e}", file=sys.stderr, flush=True)
        if msg.get("chat"):
            tg_call(
                token,
                "sendMessage",
                {
                    "chat_id": msg["chat"]["id"],
                    "text": _t("digest_save_error"),
                },
            )
        return

    # 3. Mark the chosen button with a ✅ (an edit failure is not critical).
    # Redraw from the keyboard the message already carries: the tiered digest
    # puts several vacancies' rows on one message, and only the tapped row
    # may change.
    if title is not None and msg.get("message_id"):
        try:
            tg_call(
                token,
                "editMessageReplyMarkup",
                {
                    "chat_id": msg["chat"]["id"],
                    "message_id": msg["message_id"],
                    "reply_markup": rebuild_markup(msg.get("reply_markup"), vac_id, status),
                },
            )
        except RuntimeError as e:
            if "message is not modified" not in str(e):
                print(f"WARN edit keyboard: {e}", file=sys.stderr, flush=True)
    print(f"{status}: {title or vac_id} (tg msg {msg.get('message_id')})", flush=True)


def cmd_poll(args):
    token, db_url, chat_id = get_config()
    conn = db_connect(db_url)
    state_file = os.environ.get("DIGEST_STATE_FILE", DEFAULT_STATE_FILE)
    offset = load_offset(state_file)
    print(f"Poller started (offset={offset}, loop={args.loop})", flush=True)

    while True:
        try:
            payload = {"timeout": args.timeout, "allowed_updates": ["callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = tg_call(token, "getUpdates", payload, timeout=args.timeout + 15)
            for upd in updates:
                offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                if cb:
                    try:
                        handle_callback(conn, token, cb, allowed_user=chat_id)
                    except Exception as e:
                        print(f"ERROR callback: {e}", file=sys.stderr, flush=True)
                save_offset(state_file, offset)
        except Exception as e:
            if "Unauthorized" in str(e) or "bot was blocked" in str(e):
                sys.exit(f"FATAL: token is dead or the bot is blocked: {e}")
            print(f"ERROR poll: {e}", file=sys.stderr, flush=True)
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5)
            try:
                conn = db_connect(db_url)
            except Exception as e2:
                print(f"ERROR reconnect: {e2}", file=sys.stderr, flush=True)
        if not args.loop:
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_send = sub.add_parser("send", help="send the digest")
    p_send.add_argument(
        "--limit", type=int, default=int(os.environ.get("DIGEST_LIMIT", _DIGEST["default_limit"]))
    )
    p_send.add_argument(
        "--min-score",
        type=int,
        default=int(os.environ.get("DIGEST_MIN_SCORE", _DIGEST["mid_min_score"])),
        help="floor of the mid-score tier (tier 1 starts at hot_vacancy_score)",
    )
    p_send.add_argument("--dry-run", action="store_true")
    p_send.set_defaults(func=cmd_send)

    p_alert = sub.add_parser("alert", help="send only the loud expiring-role alerts")
    p_alert.add_argument("--dry-run", action="store_true")
    p_alert.set_defaults(func=cmd_alert)

    p_poll = sub.add_parser("poll", help="listen for button taps")
    p_poll.add_argument("--loop", action="store_true", help="run forever")
    p_poll.add_argument("--timeout", type=int, default=50)
    p_poll.set_defaults(func=cmd_poll)

    args = parser.parse_args()
    load_dotenv_fallback()
    args.func(args)


if __name__ == "__main__":
    main()

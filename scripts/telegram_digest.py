#!/usr/bin/env python3
"""Telegram vacancy digest with 👍/👎 buttons.

Two modes:
  send  — pick N fresh vacancies (status=unseen, company active, not yet sent),
          post them to Telegram as separate messages with inline buttons, and
          stamp digest_sent_at.
  poll  — long-poll getUpdates: button taps write vacancy.status
          (liked/passed) to the DB and the keyboard is redrawn with a ✅ mark.

Runs anywhere with Python + psycopg2. Typical setup: run `poll --loop` as a
long-lived daemon (e.g. a systemd service or supervisor process) so taps are
always captured, and run `send` from a scheduler / cron once a day. Only one
process per bot token may call getUpdates at a time.

Configuration (env vars, or a .env file next to this script):
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
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_STATE_FILE = "~/.jobsearch_digest_state.json"
SUMMARY_FALLBACK_CHARS = 600
SUMMARY_MAX_CHARS = 1500  # llm_summary is unbounded in the DB — guard the Telegram limit
MESSAGE_MAX_CHARS = 4000
CALLBACK_PREFIX = "v"
ACTION_TO_STATUS = {"l": "liked", "p": "passed"}
STATUS_LABEL = {"liked": "👍 Liked", "passed": "👎 Passed"}

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
# A separate digest section without buttons — just a line with a link, so a
# deadline isn't missed while the company sits in the review queue.
HOT_VACANCY_SCORE = 55
DEADLINE_SOON_DAYS = 7
SELECT_CANDIDATE_HOT_SQL = """
SELECT v.id, v.title, c.canonical_name AS org, v.llm_score,
       v.locations, v.deadline
FROM vacancy v
JOIN company c ON v.company_id = c.id
WHERE c.status = 'candidate'
  AND v.llm_score IS NOT NULL
  AND v.llm_score >= %s
  AND v.status NOT IN ('passed', 'skipped', 'archived')
ORDER BY v.llm_score DESC, v.created_at DESC
LIMIT %s
"""


# --- env / config -----------------------------------------------------------

def load_dotenv_fallback():
    """Pick up a .env next to this script — for hosts without a shell profile."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def get_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    db_url = os.environ.get("SUPABASE_DB_URL")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set in the environment")
    if not db_url:
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
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
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
    return parts[0] if parts else "location not specified"


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
        return f"deadline {dl.day:02d}.{dl.month:02d}"
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


def build_message(row, index=None):
    """HTML text of a single-vacancy message."""
    org = html.escape(row.get("org") or "")
    title = html.escape(row.get("title") or "")
    prefix = f"{index}. " if index else ""
    lines = [f"<b>{prefix}{org} — {title}</b>"]

    meta = [f"📍 {html.escape(vacancy_location(row))}"]
    if row.get("llm_score") is not None:
        meta.append(f"🎯 {row['llm_score']}/100")
    if row.get("compensation"):
        meta.append(f"💰 {html.escape(str(row['compensation']))}")
    lines.append(" · ".join(meta))

    summary = vacancy_summary(row)
    if summary:
        lines.append("")
        lines.append(html.escape(summary))

    url = vacancy_url(row)
    if url:
        lines.append("")
        lines.append(f'<a href="{html.escape(url, quote=True)}">Open vacancy →</a>')
    text = "\n".join(lines)
    if len(text) > MESSAGE_MAX_CHARS:
        # trim only the summary — the link and the title must survive
        overflow = len(text) - MESSAGE_MAX_CHARS
        summary = html.escape(vacancy_summary(row))
        keep = max(200, len(summary) - overflow - 1)
        lines = [l if l != summary else _truncate(summary, keep) for l in lines]
        text = "\n".join(lines)
    return text


def build_candidate_hot_message(rows):
    """HTML section "unreviewed companies with score N+" — link lines, no
    buttons. Each line: company — vacancy (score), [⏰ deadline], link."""
    lines = [
        f"🔥 <b>Unreviewed companies with score {HOT_VACANCY_SCORE}+</b>",
        "These companies aren't reviewed yet — take a look before the deadline passes.",
        "",
    ]
    for row in rows:
        org = html.escape(row.get("org") or "")
        title = html.escape(row.get("title") or "")
        score = row.get("llm_score")
        bits = [f"<b>{org}</b> — {title}"]
        if score is not None:
            bits.append(f"🎯 {score}")
        dl = deadline_soon_label(row.get("deadline"))
        if dl:
            bits.append(f"⏰ {html.escape(dl)}")
        line = " · ".join(bits)
        url = vacancy_url(row)
        if url:
            line += f' — <a href="{html.escape(url, quote=True)}">open →</a>'
        lines.append(line)
    return "\n".join(lines)


def build_keyboard(vac_id, chosen=None):
    """👍/👎 buttons; the chosen one gets a ✅ (tapping again flips the choice)."""
    like = "👍 Liked"
    pas = "👎 Passed"
    if chosen == "liked":
        like = "✅ " + like
    elif chosen == "passed":
        pas = "✅ " + pas
    return {
        "inline_keyboard": [[
            {"text": like, "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:l"},
            {"text": pas, "callback_data": f"{CALLBACK_PREFIX}:{vac_id}:p"},
        ]]
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
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    return conn


def fetch_fresh(conn, limit, min_score):
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(SELECT_FRESH_SQL, (min_score, limit))
        return [dict(r) for r in cur.fetchall()]


def fetch_candidate_hot(conn, limit=10, min_score=HOT_VACANCY_SCORE):
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(SELECT_CANDIDATE_HOT_SQL, (min_score, limit))
        return [dict(r) for r in cur.fetchall()]


def mark_sent(conn, vac_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancy SET digest_sent_at = now() WHERE id = %s", (vac_id,)
        )


def unmark_sent(conn, vac_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE vacancy SET digest_sent_at = NULL WHERE id = %s", (vac_id,)
        )


def set_status(conn, vac_id, status):
    """Returns the title, or None when the vacancy isn't found."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE vacancy SET status = %s, status_updated_at = now()
               WHERE id = %s RETURNING title""",
            (status, vac_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


# --- send mode ----------------------------------------------------------------

def cmd_send(args):
    token, db_url, chat_id = get_config()
    conn = db_connect(db_url)
    rows = fetch_fresh(conn, args.limit, args.min_score)
    # Strong vacancies at unreviewed companies — a separate section.
    hot_rows = fetch_candidate_hot(conn)

    if not rows and not hot_rows:
        if not args.dry_run:
            tg_call(token, "sendMessage", {
                "chat_id": chat_id,
                "text": "🗞 No fresh vacancies for the digest today.",
            })
        print("No candidates — sent a 'nothing today' notice.", flush=True)
        return

    if args.dry_run:
        for i, row in enumerate(rows, 1):
            print(f"--- {i} ---")
            print(build_message(row, i))
        if hot_rows:
            print("--- candidate hot section ---")
            print(build_candidate_hot_message(hot_rows))
        print(
            f"\n[dry-run] {len(rows)} main + {len(hot_rows)} unreviewed, "
            f"nothing sent", flush=True,
        )
        return

    if not rows:
        # Only the unreviewed-companies section — no fresh main vacancies.
        tg_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": build_candidate_hot_message(hot_rows),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        print(
            f"No fresh ones; sent the unreviewed section ({len(hot_rows)}).",
            flush=True,
        )
        return

    tg_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"🗞 <b>Vacancy digest</b> — {len(rows)} fresh. Tap 👍 or 👎 under each.",
        "parse_mode": "HTML",
    })
    sent = 0
    for i, row in enumerate(rows, 1):
        mark_sent(conn, row["id"])  # claim-first: a parallel run won't send a dupe
        try:
            tg_call(token, "sendMessage", {
                "chat_id": chat_id,
                "text": build_message(row, i),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": build_keyboard(row["id"]),
            })
            sent += 1
            time.sleep(0.5)  # respect the rate limit
        except Exception as e:
            unmark_sent(conn, row["id"])  # return it to tomorrow's queue
            print(f"ERROR on '{row.get('title')}': {e}", file=sys.stderr, flush=True)
    print(f"Sent {sent}/{len(rows)} vacancies to chat {chat_id}", flush=True)

    # The unreviewed-companies section goes as one message without buttons.
    if hot_rows:
        try:
            tg_call(token, "sendMessage", {
                "chat_id": chat_id,
                "text": build_candidate_hot_message(hot_rows),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            print(
                f"Unreviewed-companies section sent ({len(hot_rows)}).",
                flush=True,
            )
        except Exception as e:
            print(f"ERROR unreviewed section: {e}", file=sys.stderr, flush=True)


# --- poll mode ----------------------------------------------------------------

def load_offset(state_file):
    try:
        return json.loads(Path(state_file).expanduser().read_text())["offset"]
    except Exception:
        return None


def save_offset(state_file, offset):
    p = Path(state_file).expanduser()
    p.write_text(json.dumps({"offset": offset}))


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
        ack["text"] = f"Recorded: {STATUS_LABEL[parsed[1]]}"
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
            tg_call(token, "sendMessage", {
                "chat_id": msg["chat"]["id"],
                "text": "⚠️ Couldn't save your choice, please tap again.",
            })
        return

    # 3. Mark the chosen button with a ✅ (an edit failure is not critical).
    if title is not None and msg.get("message_id"):
        try:
            tg_call(token, "editMessageReplyMarkup", {
                "chat_id": msg["chat"]["id"],
                "message_id": msg["message_id"],
                "reply_markup": build_keyboard(vac_id, chosen=status),
            })
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
    p_send.add_argument("--limit", type=int, default=5)
    p_send.add_argument("--min-score", type=int, default=40)
    p_send.add_argument("--dry-run", action="store_true")
    p_send.set_defaults(func=cmd_send)

    p_poll = sub.add_parser("poll", help="listen for button taps")
    p_poll.add_argument("--loop", action="store_true", help="run forever")
    p_poll.add_argument("--timeout", type=int, default=50)
    p_poll.set_defaults(func=cmd_poll)

    args = parser.parse_args()
    load_dotenv_fallback()
    args.func(args)


if __name__ == "__main__":
    main()

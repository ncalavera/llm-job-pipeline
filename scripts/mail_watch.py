"""Gmail watcher: one Telegram alert per incoming hiring email.

Runs every 10 minutes on the server (deploy/forge/jobsearch-mail-watch.timer).
Each run lists mail of the last two days, drops ids already in the state file, matches
the rest against a small TOML rules file (sender domains + subject phrases),
sends one Telegram message per match, and records every id as seen. Rules,
not a model, decide; polling, not Gmail push — both settled in the plan.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (from ~/jobsearch/.env),
GMAIL_TOKEN_FILE, MAIL_WATCH_RULES, MAIL_WATCH_STATE_FILE.

Usage:
  mail_watch.py                 one poll (seed run when the state file is absent)
  mail_watch.py --dry-run       print what would be sent, write nothing
  mail_watch.py --dry-run --since-days 90
                                replay all mail of the last N days with reasons
  mail_watch.py --query "newer_than:7d -in:sent"
                                poll with another Gmail query (live tests)
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nightly_run import mask_secrets  # noqa: E402
from telegram_digest import read_state_file, tg_call, update_state_file  # noqa: E402

DEFAULT_TOKEN_FILE = "~/Projects/tools/google-vibe-api/.secrets/token.json"
DEFAULT_RULES = "~/jobsearch/mail_watch_rules.toml"
DEFAULT_STATE = "~/jobsearch/mail_watch_state.json"
# All incoming mail, not in:inbox — Gmail filters archive most mail on arrival.
QUERY = "newer_than:2d -in:sent -in:spam -in:trash -in:draft"
SEEN_TTL_S = 7 * 86400
MAX_SENDS_PER_RUN = 20
SNIPPET_CHARS = 300
BATCH_SIZE = 20  # ponytail: Gmail 429s on bigger/faster batches; tune here
BATCH_RETRIES = 3
BATCH_BACKOFF_S = 2
HTTP_TIMEOUT_S = 30
ESCALATE_AFTER = 3
ESCALATE_EVERY_S = 6 * 3600
RULE_KEYS = (
    "own_addresses",
    "platform_domains",
    "org_domains",
    "subject_phrases",
    "exclude_domains",
)

_extra_secrets: list[str] = []


def mask(text) -> str:
    out = mask_secrets(str(text))
    for s in _extra_secrets:
        out = out.replace(s, "***")
    return out


def log(msg: str) -> None:
    print(mask(msg), flush=True)


# --- rules -------------------------------------------------------------------


def load_rules(path) -> dict:
    p = Path(path).expanduser()
    try:
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise FileNotFoundError(f"rules file not found: {p}") from None
    missing = [k for k in RULE_KEYS if k not in data]
    if missing:
        raise KeyError(f"rules file {p} missing keys: {', '.join(missing)}")
    return {k: [str(v).strip().lower() for v in data[k]] for k in RULE_KEYS}


def _domain_in(domain: str, rules: list[str]) -> bool:
    return any(domain == r or domain.endswith("." + r) for r in rules)


def classify(from_header: str, subject: str, rules: dict) -> str | None:
    """Reason string when the mail should alert, else None."""
    _, address = email.utils.parseaddr(from_header or "")
    address = address.lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    subject_l = (subject or "").lower()
    if address in rules["own_addresses"]:
        return None
    if address in rules["exclude_domains"] or _domain_in(domain, rules["exclude_domains"]):
        return None
    if _domain_in(domain, rules["platform_domains"]):
        return "platform_domain"
    if _domain_in(domain, rules["org_domains"]):
        return "org_domain"
    for phrase in rules["subject_phrases"]:
        if phrase and phrase in subject_l:
            return f"subject:{phrase}"
    return None


# --- gmail -------------------------------------------------------------------


def gmail_service(token_path):
    """Build the Gmail client from the token file. Side effect: registers the
    token's secret values with mask(), so every later log line redacts them."""
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    from httplib2 import Http

    info = json.loads(Path(token_path).expanduser().read_text())
    _extra_secrets.extend(
        v for k in ("token", "refresh_token", "client_secret") if (v := info.get(k)) and len(v) >= 6
    )
    creds = Credentials.from_authorized_user_info(info)
    # Explicit socket timeout: a hung Gmail call must fail inside the run, not
    # sit until systemd's TimeoutStartSec kills it without a counted failure.
    return build(
        "gmail",
        "v1",
        http=AuthorizedHttp(creds, http=Http(timeout=HTTP_TIMEOUT_S)),
        cache_discovery=False,
    )


def fetch_new(service, seen: dict, query: str = QUERY, replay: bool = False) -> list[dict]:
    """Metadata dicts (id, threadId, internalDate, from, subject, snippet) for
    listed messages not yet seen. Follows every page; reads metadata in
    small batches (BATCH_SIZE) with backoff on 429."""
    ids, token = [], None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100, pageToken=token)
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    if not replay:
        ids = [i for i in ids if i not in seen]
    out = []
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        results = _get_batch(service, chunk)
        for i in chunk:
            m = results.get(i) or {}
            headers = {
                h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])
            }
            out.append(
                {
                    "id": i,
                    "threadId": m.get("threadId", ""),
                    "internalDate": int(m.get("internalDate", 0)),
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "snippet": m.get("snippet", ""),
                }
            )
    return out


def _get_batch(service, ids: list[str]) -> dict:
    """One Gmail batch request for the metadata of ``ids``. Gmail answers a
    large or fast batch with 429 "Too many concurrent requests"; retry the
    failed ids with a short backoff instead of failing the run."""
    results, pending = {}, list(ids)
    for attempt in range(BATCH_RETRIES + 1):
        errors = {}

        def _cb(request_id, response, exception):
            if exception:
                errors[request_id] = exception
            else:
                results[request_id] = response

        batch = service.new_batch_http_request(callback=_cb)
        for i in pending:
            batch.add(
                service.users()
                .messages()
                .get(userId="me", id=i, format="metadata", metadataHeaders=["From", "Subject"]),
                request_id=i,
            )
        batch.execute()
        if not errors:
            return results
        retryable = [
            i for i, e in errors.items() if getattr(e, "status_code", None) in (429, 500, 503)
        ]
        if len(retryable) < len(errors) or attempt == BATCH_RETRIES:
            raise next(iter(errors.values()))
        pending = retryable
        time.sleep(BATCH_BACKOFF_S * (attempt + 1))
    return results


# --- telegram ----------------------------------------------------------------


def build_message(meta: dict, reason: str) -> str:
    snippet = html.unescape(meta.get("snippet", ""))[:SNIPPET_CHARS]
    link = f"https://mail.google.com/mail/u/0/#inbox/{meta.get('threadId', '')}"
    return (
        f"📩 <b>{html.escape(meta.get('subject') or '(no subject)')}</b>\n"
        f"{html.escape(meta.get('from', ''))}\n\n"
        f"{html.escape(snippet)}\n\n"
        f'<a href="{html.escape(link, quote=True)}">Open in Gmail</a> · {html.escape(reason)}'
    )


def telegram_send(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
    tg_call(
        token,
        "sendMessage",
        {
            "chat_id": chat,
            "text": mask(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


# --- run ---------------------------------------------------------------------


def _load_state(state_path) -> dict | None:
    """None when the file is absent (seed mode); raises on a corrupt file."""
    try:
        return json.loads(Path(state_path).expanduser().read_text())
    except FileNotFoundError:
        return None


def replay(rules: dict, service, since_days: int) -> int:
    """Diagnostic: print every message of the last N days with its reason.
    Reads no state, sends nothing. Returns the number listed."""
    metas = fetch_new(
        service, {}, query=f"newer_than:{since_days}d -in:spam -in:trash", replay=True
    )
    for m in metas:
        reason = classify(m["from"], m["subject"], rules) or "no match"
        print(f"{reason:24} | {m['from'][:50]:50} | {m['subject'][:70]}")
    return len(metas)


def run_once(
    rules: dict, state_path, service, send, dry_run: bool = False, now=None, query: str = QUERY
) -> dict:
    now = now or time.time()
    state = _load_state(state_path)
    state = state or {}
    seed = "seeded_at" not in state  # a state file holding only failure fields is still unseeded
    seen = {k: v for k, v in state.get("seen", {}).items() if now * 1000 - v < SEEN_TTL_S * 1000}
    metas = fetch_new(service, seen, query=query)

    if seed:
        seen.update({m["id"]: m["internalDate"] for m in metas})
        if not dry_run:
            update_state_file(state_path, seen=seen, seeded_at=now)
        log(f"seed: recorded {len(metas)} messages, sent 0")
        return {"listed": len(metas), "sent": 0, "seed": True}

    sent = matched = 0
    for m in sorted(metas, key=lambda x: x["internalDate"]):
        reason = classify(m["from"], m["subject"], rules)
        if reason:
            matched += 1
            if sent >= MAX_SENDS_PER_RUN:
                continue  # stays unseen; goes out next run
            if dry_run:
                print(
                    f"would send: id={m['id']} reason={reason} | {m['from'][:50]} | {m['subject'][:70]}"
                )
            else:
                send(build_message(m, reason))
                seen[m["id"]] = m["internalDate"]
                update_state_file(state_path, seen=seen)
                log(f"sent 1: id={m['id']} reason={reason}")
            sent += 1
        else:
            seen[m["id"]] = m["internalDate"]
    if not dry_run:
        update_state_file(state_path, seen=seen)
    log(f"run: listed {len(metas)} new, matched {matched}, sent {sent}")
    return {"listed": len(metas), "matched": matched, "sent": sent, "seed": False}


def record_failure(state_path, err: str, send, now=None) -> None:
    """Per KTD6: count consecutive failures; escalate at 3, then every 6 h."""
    now = now or time.time()
    state = read_state_file(state_path)
    n = int(state.get("consecutive_failures", 0)) + 1
    last = float(state.get("last_escalation_at", 0) or 0)
    fields = {"consecutive_failures": n, "last_error": mask(err)[:500]}
    if n >= ESCALATE_AFTER and (not last or now - last >= ESCALATE_EVERY_S):
        try:
            send(
                f"⚠️ mail watcher is failing ({n} runs in a row)\n<code>{html.escape(mask(err)[:800])}</code>"
            )
            fields["last_escalation_at"] = now
        except Exception as e:  # Telegram itself is down: the journal carries it
            log(f"escalation send failed: {e.__class__.__name__}: {e}")
    update_state_file(state_path, **fields)


def _error_text(e: BaseException) -> str:
    body = getattr(e, "content", None)
    body = body.decode(errors="replace") if isinstance(body, bytes) else (body or "")
    return f"{e.__class__.__name__}: {e} {body[:600]}".strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since-days", type=int, help="replay the last N days (dry-run only)")
    ap.add_argument("--query", default=QUERY, help=f"Gmail query for one poll (default: {QUERY})")
    args = ap.parse_args(argv)
    if args.since_days and not args.dry_run:
        ap.error("--since-days requires --dry-run")
    state_path = os.environ.get("MAIL_WATCH_STATE_FILE", DEFAULT_STATE)
    try:
        rules = load_rules(os.environ.get("MAIL_WATCH_RULES", DEFAULT_RULES))
        service = gmail_service(os.environ.get("GMAIL_TOKEN_FILE", DEFAULT_TOKEN_FILE))
        if args.since_days:
            replay(rules, service, args.since_days)
        else:
            run_once(
                rules, state_path, service, telegram_send, dry_run=args.dry_run, query=args.query
            )
        if not args.dry_run:
            update_state_file(state_path, consecutive_failures=0)
        return 0
    except Exception as e:  # noqa: BLE001 — every failure is logged and counted
        err = _error_text(e)
        log(f"run failed: {err}")
        if not args.dry_run:
            record_failure(state_path, err, telegram_send)
        return 1


if __name__ == "__main__":
    sys.exit(main())

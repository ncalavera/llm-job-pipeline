"""Reconcile 80,000 Hours newsletter role links with source observations."""

from __future__ import annotations

import html
import json
import re
import email.utils
import os
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from source_observations import (
    lookup_source_urls,
    record_source_observations,
    record_source_run,
)

NEWSLETTER_SOURCE = "80k_newsletter"
BOARD_SOURCE_KEYS = ("80k_hours", "80_000_hours")
BOARD_HOST = "jobs.80000hours.org"
MAILCHIMP_HOSTS = {"mailchi.mp", "click.mailchi.mp", "list-manage.com"}
MAILCHIMP_TRACK_HOSTS = {
    "us.list-manage.com",
    "80000hours.us2.list-manage.com",
    "80000hours.us11.list-manage.com",
}
DEFAULT_STATE = "~/jobsearch/newsletter_reconcile.json"
MAX_BODY_BYTES = 2_000_000


def is_newsletter(meta: dict) -> bool:
    """Recognize only the 80k role newsletter, by sender and subject."""
    _, sender = email.utils.parseaddr(meta.get("from") or "")
    if not sender:
        sender = (re.search(r"[\w.+-]+@[\w.-]+", meta.get("from") or "") or [""])[0]
    sender = sender.lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    subject = (meta.get("subject") or "").lower()
    return (domain == "80000hours.org" or domain.endswith(".80000hours.org")) and bool(
        re.search(r"\b\d+\s+new roles?\b", subject)
    )


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href")
        self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href, self._text = None, []


def _parts(payload: dict):
    body = payload.get("body", {})
    if body.get("data"):
        import base64

        yield base64.urlsafe_b64decode(body["data"] + "=" * (-len(body["data"]) % 4)).decode(
            errors="replace"
        )
    for part in payload.get("parts", []):
        yield from _parts(part)


def _allowed(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and (p.hostname or "").lower() == BOARD_HOST


def _job_id(url: str) -> str | None:
    if not _allowed(url):
        return None
    value = parse_qs(urlparse(url).query).get("jobPk", [None])[0]
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def _redirect_target(url: str) -> str | None:
    p = urlparse(url)
    if p.scheme != "https" or (
        (p.hostname or "").lower() not in MAILCHIMP_HOSTS
        and (p.hostname or "").lower() not in MAILCHIMP_TRACK_HOSTS
    ):
        return None
    query = parse_qs(p.query)
    for key in ("url", "u", "redirect", "href", "link"):
        for value in query.get(key, []):
            value = unquote(value)
            if _allowed(value):
                return value
    host = (p.hostname or "").lower()
    tracking_path = p.path.startswith("/track/click") or (
        host in MAILCHIMP_TRACK_HOSTS and bool(re.fullmatch(r"/[A-Za-z0-9_-]{8,}", p.path))
    )
    if host in MAILCHIMP_TRACK_HOSTS and tracking_path:

        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

            def http_error_302(self, req, fp, code, msg, headers):
                return fp

            http_error_301 = http_error_302
            http_error_303 = http_error_302
            http_error_307 = http_error_302

        try:
            req = Request(url, headers={"User-Agent": "job-pipeline-newsletter-audit"})
            with build_opener(_NoRedirect).open(req, timeout=10) as response:
                location = response.headers.get("Location", "")
            return location if _job_id(location) else None
        except Exception:
            return None
    return None


def extract_links(body: str) -> list[dict]:
    """Extract unique role links; navigation, actions and unsubscribe links fall out."""
    parser = _Links()
    parser.feed(body or "")
    links = list(parser.links)
    links.extend((u, "") for u in re.findall(r"https://[^\s<>\"]+", body or ""))
    out, seen, seen_raw = [], set(), set()
    for raw, title in links:
        raw = html.unescape(raw).strip()
        if raw in seen_raw:
            continue
        seen_raw.add(raw)
        target = raw if _job_id(raw) else _redirect_target(raw)
        job_pk = _job_id(target or "")
        if not job_pk or job_pk in seen:
            continue
        seen.add(job_pk)
        out.append({"external_id": job_pk, "title": re.sub(r"\s+", " ", title), "url": target})
    return out


def _body(service, message_id: str) -> str:
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    text = "\n".join(_parts(message.get("payload", {})))
    if len(text.encode()) > MAX_BODY_BYTES:
        raise ValueError("newsletter body exceeds limit")
    return text


def reconcile_message(meta: dict, service, state_path=DEFAULT_STATE, now=None) -> dict:
    """Persist one newsletter's raw links and match outcomes. No notifications."""
    if not is_newsletter(meta):
        return {"status": "ignored", "matched": 0, "unverified": 0}
    path = Path(state_path).expanduser()
    state = json.loads(path.read_text()) if path.exists() else {"messages": {}}
    key = str(meta.get("id") or "")
    cached = state.setdefault("messages", {}).get(key)
    if (
        cached
        and cached.get("status") == "complete"
        and not any(item.get("outcome") == "unverified" for item in cached.get("links", []))
    ):
        return cached
    run_id = f"newsletter-{key[:32]}"
    source_url = "https://jobs.80000hours.org"
    if cached and cached.get("links"):
        links = cached["links"]
    else:
        try:
            links = extract_links(_body(service, key))
        except Exception as exc:
            record_source_run(run_id, NEWSLETTER_SOURCE, source_url, complete=False, error=str(exc))
            raise
    if not links:
        error = "newsletter body contained no role links"
        record_source_run(run_id, NEWSLETTER_SOURCE, source_url, complete=False, error=error)
        result = {"status": "partial", "matched": 0, "unverified": 0, "links": [], "error": error}
        state["messages"][key] = result
        _write_state(path, state)
        return result
    observed = []
    matched = unverified = 0
    known = lookup_source_urls(BOARD_SOURCE_KEYS, [item["external_id"] for item in links])
    for item in links:
        found = item["external_id"] in known
        item.update({"outcome": "matched" if found else "unverified", "reason": None})
        matched += found
        unverified += not found
        observed.append(item)
    ok = record_source_observations(run_id, NEWSLETTER_SOURCE, source_url, observed)
    ok = (
        record_source_run(
            run_id,
            NEWSLETTER_SOURCE,
            source_url,
            raw_count=len(observed),
            accepted_count=matched,
            excluded_count=unverified,
            complete=ok,
            error=None if ok else "source ledger write failed",
        )
        and ok
    )
    result = {
        "status": "complete" if ok else "partial",
        "matched": matched,
        "unverified": unverified,
        "links": observed,
    }
    state["messages"][key] = result
    _write_state(path, state)
    return result


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".newsletter-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

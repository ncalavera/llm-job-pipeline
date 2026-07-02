"""Local dashboard server — easy mode, no Vercel, stdlib only.

Serves the existing ``public/`` dashboard on localhost and implements the four
API endpoints the dashboard JS calls (``/api/statuses``, ``/api/company-statuses``,
``/api/save``, ``/api/company-review``) directly against the data-access layer.
Works on the SQLite backend out of the box; if SUPABASE_DB_URL is set it talks
to Supabase instead — same code path.

Run:
    python3 scripts/dashboard_local.py            # serves on http://127.0.0.1:8000
    python3 scripts/dashboard_local.py --port 9000
    python3 scripts/dashboard_local.py --no-browser

The dashboard is served over http:// (not https://), so the frontend uses
relative API paths (see public/modules/state.js) that this server answers.
"""

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = PROJECT_ROOT / "public"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# DAL is imported lazily inside handlers so a missing dependency surfaces as a
# clean 500 rather than a server that won't boot.

VALID_STATUSES = {
    "unseen",
    "liked",
    "passed",
    "to_apply",
    "to_research",
    "to_network",
    "skipped",
    "applied",
    "expiring",
    "archived",
}
VALID_ACTIONS = {"approve", "reject"}

_COMPANY_REVIEW_MAP = {"active": "approved", "candidate": "pending", "inactive": "rejected"}

# The server is single-threaded (HTTPServer, not ThreadingHTTPServer): the DAL
# connection is a singleton and the SQLite backend opens its connection with
# check_same_thread=True, so a connection created on one thread cannot be used
# on another. Serving requests sequentially keeps every DB call on one thread
# and is plenty for a localhost single-user dashboard. The lock is kept as a
# defensive no-op guard around DB access (harmless, uncontended).
_db_lock = threading.Lock()


def _commit():
    from db_conn import get_conn

    get_conn().commit()


class Handler(BaseHTTPRequestHandler):
    # -- helpers -----------------------------------------------------------

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/statuses":
            return self._api_statuses()
        if path == "/api/company-statuses":
            return self._api_company_statuses()
        if path == "/api/health":
            return self._send_json(200, {"ok": True})
        return self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/save":
            return self._api_save()
        if path == "/api/company-review":
            return self._api_company_review()
        return self._send_json(404, {"error": "Not found"})

    # -- static files ------------------------------------------------------

    def _serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        target = (PUBLIC_DIR / rel).resolve()
        # Path traversal guard.
        if not str(target).startswith(str(PUBLIC_DIR.resolve())):
            return self._send_json(403, {"error": "Forbidden"})
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        ctype = _content_type(target.suffix)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- API endpoints -----------------------------------------------------

    def _api_statuses(self):
        try:
            from database_supabase import load_vacancies

            with _db_lock:
                vacs = load_vacancies(include_inactive_companies=True, light=True)
            statuses, timestamps = {}, {}
            for vid, v in vacs.items():
                st = v.get("status")
                if st in ("unseen", "archived"):
                    continue
                statuses[vid] = st
                ts = v.get("status_updated_at")
                if ts:
                    timestamps[vid] = ts
            return self._send_json(200, {"statuses": statuses, "timestamps": timestamps})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"  statuses error: {exc}\n")
            return self._send_json(500, {"error": "Database error"})

    def _api_company_statuses(self):
        try:
            from db_conn import get_conn

            with _db_lock:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT id, status FROM company")
                rows = cur.fetchall()
                cur.close()
            statuses = {str(cid): _COMPANY_REVIEW_MAP.get(st, "pending") for cid, st in rows}
            return self._send_json(200, {"statuses": statuses})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"  company-statuses error: {exc}\n")
            return self._send_json(500, {"error": "Database error"})

    def _api_save(self):
        body = self._read_body()
        vid = body.get("id")
        status = body.get("status")
        if not vid or not status:
            return self._send_json(400, {"error": "Missing id or status"})
        if status not in VALID_STATUSES:
            return self._send_json(400, {"error": "Invalid status"})
        try:
            from db_conn import get_conn
            from database_supabase import update_vacancy_status

            with _db_lock:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT id FROM vacancy WHERE id = %s", (vid,))
                if cur.fetchone() is None:
                    cur.close()
                    return self._send_json(404, {"error": "Vacancy not found", "id": vid})
                cur.close()
                update_vacancy_status(vid, status)
                _commit()
            return self._send_json(200, {"ok": True})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"  save error: {exc}\n")
            return self._send_json(500, {"error": "Database error"})

    def _api_company_review(self):
        body = self._read_body()
        company_id = body.get("company_id")
        action = body.get("action")
        if not company_id or not action:
            return self._send_json(400, {"error": "Missing company_id or action"})
        if action not in VALID_ACTIONS:
            return self._send_json(400, {"error": "Invalid action"})
        new_status = "active" if action == "approve" else "inactive"
        reason = f"{action}d via local dashboard"
        try:
            from db_conn import get_conn

            with _db_lock:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT id FROM company WHERE id = %s", (company_id,))
                if cur.fetchone() is None:
                    cur.close()
                    return self._send_json(
                        404, {"error": "Company not found", "company_id": company_id}
                    )
                cur.execute(
                    "UPDATE company SET status = %s, status_reason = %s WHERE id = %s",
                    (new_status, reason, company_id),
                )
                cur.close()
                _commit()
            return self._send_json(200, {"ok": True, "action": action, "company_id": company_id})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"  company-review error: {exc}\n")
            return self._send_json(500, {"error": "Database error"})


def _content_type(suffix: str) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }.get(suffix.lower(), "application/octet-stream")


def main():
    parser = argparse.ArgumentParser(description="Serve the dashboard locally against the local DB")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    args = parser.parse_args()

    data_js = PUBLIC_DIR / "data.js"
    if not data_js.exists():
        sys.stderr.write(
            "  Warning: public/data.js not found. Run "
            "`python3 scripts/fetch_vacancies.py --report-only` first.\n"
        )

    backend = (
        "SQLite (local)"
        if not (os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL"))
        else "Supabase"
    )

    server = HTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"  Dashboard backend: {backend}")
    print(f"  Serving {PUBLIC_DIR}")
    print(f"  Open: {url}")
    print("  Press Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()

"""Connection singleton for Supabase (psycopg2).

Zero project imports — safe to import from any module without circular deps.
"""

import os
import sys

import psycopg2

_conn = None


def get_conn():
    """Lazy singleton psycopg2 connection. Prints DB info on first connect.

    Reads SUPABASE_DB_URL from env. Single retry on connect, then fail-fast.
    Auto-reconnects if server closed the connection (e.g. PgBouncer timeout).
    """
    global _conn
    if _conn is not None and _conn.closed == 0:
        # Verify connection is actually alive (PgBouncer may have killed it)
        try:
            cur = _conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return _conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            print("  Supabase: reconnecting (server closed connection)")
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None

    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")
    if not db_url:
        print("ERROR: Set SUPABASE_DB_URL env var", file=sys.stderr)
        sys.exit(1)

    for attempt in range(2):
        try:
            _conn = psycopg2.connect(
                db_url,
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
                options="-c idle_in_transaction_session_timeout=300000 -c statement_timeout=60000",
            )
            _conn.autocommit = False
            cur = _conn.cursor()
            # Session Pooler strips libpq `options`, so apply timeouts via SET.
            # statement_timeout prevents any single query from hanging forever;
            # TCP keepalives (set in connect) detect dead connections.
            cur.execute("SET statement_timeout = 60000")
            cur.execute("SET idle_in_transaction_session_timeout = 300000")
            _conn.commit()
            cur.execute("SELECT current_database(), current_user")
            db_name, db_user = cur.fetchone()
            print(f"  Supabase: connected ({db_name}, {db_user})")
            cur.close()
            return _conn
        except psycopg2.OperationalError:
            if attempt == 0:
                continue
            raise


def close_conn():
    """Explicit cleanup."""
    global _conn
    if _conn is not None and _conn.closed == 0:
        _conn.close()
    _conn = None

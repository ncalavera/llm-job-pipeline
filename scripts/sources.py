#!/usr/bin/env python3
"""Show and manage the sources feeding the pipeline.

One screen answers "what do I have enabled?": the persisted job boards (they
survive sessions -- database_supabase.set_board_enabled) plus the tracked active
companies. Enabling / disabling a board is one subcommand, and the change is
durable: the board keeps (or stops) fetching on every future ``/jobs-new`` with
no env var and no reminder. ``JOB_BOARDS`` / ``--boards`` stays a manual override
applied ON TOP of this persisted set for a single run.

    python3 scripts/sources.py                    # list enabled boards + active companies
    python3 scripts/sources.py recommend          # boards that fit YOUR profile
    python3 scripts/sources.py enable-board <id>   # make a board stick across runs
    python3 scripts/sources.py disable-board <id>  # stop a board fetching by default

Board ids come from config (config._ALL_JOB_BOARDS); an unknown id on enable is
rejected with the known list, mirroring /jobs-add Mode B. Active-company status
is managed in /jobs-review, not here -- this command only reports it.

``recommend`` derives its suggestions from the user profile (target field, roles,
geography) via scripts/profile_targeting.py -- an engineer is proposed
engineering boards, not six impact boards. It only PROPOSES: nothing is enabled
until the user runs enable-board (STRATEGY guardrail 8).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import _ALL_JOB_BOARDS  # noqa: E402
from database_supabase import (  # noqa: E402
    BoardPersistenceUnavailable,
    get_company_fitness_map,
    get_enabled_boards,
    set_board_enabled,
)
from db_conn import close_conn  # noqa: E402


def _board_name(board_id: str) -> str:
    cfg = _ALL_JOB_BOARDS.get(board_id) or {}
    return cfg.get("name", board_id)


def cmd_list() -> int:
    enabled = get_enabled_boards()
    fitness = get_company_fitness_map()
    active = sorted(n for n, f in fitness.items() if f.get("status") == "active")

    print("Enabled job boards (persist across runs; --boards / JOB_BOARDS add more for one run):")
    if enabled:
        for bid in enabled:
            unknown = (
                "" if bid in _ALL_JOB_BOARDS else "   [unknown id — not in config, will be skipped]"
            )
            print(f"  {bid:<16} {_board_name(bid)}{unknown}")
    else:
        print("  (none — enable one:  python3 scripts/sources.py enable-board <id>)")

    print()
    print(f"Active companies (tracked; fetched every run): {len(active)}")
    for name in active:
        print(f"  {name}")

    print()
    print(
        f"Total sources feeding the pipeline: {len(enabled)} board(s) + {len(active)} company(ies)"
    )
    return 0


def cmd_recommend() -> int:
    """Propose the boards that fit the user's profile (never enables any)."""
    from profile_targeting import recommend_boards

    recs = recommend_boards()
    try:
        enabled = set(get_enabled_boards())
    except BoardPersistenceUnavailable:
        enabled = set()  # a pre-board schema still shows recommendations

    print("Boards recommended for your profile (target field + roles + geography):")
    if not recs:
        print("  (none — add a ## TARGET_ROLES section to config/user_profile.md)")
        return 0
    for r in recs:
        mark = "  [enabled]" if r["id"] in enabled else ""
        print(f"  {r['id']:<24} {r['reason']}{mark}")
    print()
    print("Enable the ones you want (they then fetch every run):")
    print("  python3 scripts/sources.py enable-board <id>")
    return 0


def cmd_enable(board_id: str) -> int:
    if board_id not in _ALL_JOB_BOARDS:
        known = ", ".join(sorted(_ALL_JOB_BOARDS)) or "(none)"
        print(f"Unknown board '{board_id}'. Known boards: {known}")
        return 2
    set_board_enabled(board_id, True)
    print(
        f"Enabled board '{board_id}' ({_board_name(board_id)}). "
        "It will fetch on every /jobs-new until you disable it."
    )
    return 0


def cmd_disable(board_id: str) -> int:
    # Disable is tolerant of an unknown id on purpose: it's the clean-up path for
    # a board that config no longer ships but the DB still has enabled.
    set_board_enabled(board_id, False)
    print(
        f"Disabled board '{board_id}'. It no longer fetches by default "
        "(add it to --boards / JOB_BOARDS for a one-off run)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Show and manage pipeline sources (enabled boards + active companies)."
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="Show enabled boards + active companies (default).")
    sub.add_parser("recommend", help="Propose boards that fit your profile (enables nothing).")
    en = sub.add_parser("enable-board", help="Persist a board as enabled (survives sessions).")
    en.add_argument("board_id")
    di = sub.add_parser("disable-board", help="Clear a board's enabled flag.")
    di.add_argument("board_id")
    args = p.parse_args(argv)

    try:
        if args.cmd == "recommend":
            return cmd_recommend()
        if args.cmd == "enable-board":
            return cmd_enable(args.board_id)
        if args.cmd == "disable-board":
            return cmd_disable(args.board_id)
        return cmd_list()
    except BoardPersistenceUnavailable as exc:
        print(exc)
        return 1
    finally:
        try:
            close_conn()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

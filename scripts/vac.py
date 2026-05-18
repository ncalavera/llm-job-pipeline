"""KISS CLI для триажа вакансий — list / show / mark / open / companies.

Запуск: python3 scripts/vac.py list --status liked
Алиас: alias vac="cd ~/Projects/personal/job-search-2026 && python3 scripts/vac.py"
"""

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from database_supabase import (
    get_conn,
    load_vacancies,
    update_vacancy_status,
)

VALID_STATUSES = {
    "unseen", "liked", "passed",
    "to_apply", "to_research", "to_network",
    "skipped", "applied",
}


def _term_width() -> int:
    return shutil.get_terminal_size((100, 20)).columns


def _ansi(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _color_score(score) -> str:
    if score is None:
        return _ansi("90", "  —")
    s = int(score)
    txt = f"{s:>3}"
    if s >= 70:
        return _ansi("32", txt)
    if s >= 50:
        return _ansi("33", txt)
    return _ansi("31", txt)


def _color_status(status: str) -> str:
    colors = {
        "liked": "32", "to_apply": "32;1", "applied": "36",
        "to_research": "34", "to_network": "34",
        "passed": "90", "skipped": "90",
        "unseen": "37",
    }
    return _ansi(colors.get(status, "0"), status or "?")


def _short_id(uid: str) -> str:
    return uid[:8]


def _resolve_uid(prefix: str, vacancies: dict) -> str | None:
    if prefix in vacancies:
        return prefix
    matches = [u for u in vacancies if u.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Префикс '{prefix}' неоднозначен — {len(matches)} совпадений.")
    return None


def _location_brief(locations) -> str:
    if not locations:
        return "—"
    parts = []
    for loc in locations[:2]:
        bits = []
        wm = loc.get("work_mode")
        if wm:
            bits.append(wm)
        city = loc.get("city")
        country = loc.get("country")
        if city:
            bits.append(city)
        elif country:
            bits.append(country)
        if bits:
            parts.append(" ".join(bits))
    return ", ".join(parts) if parts else "—"


def cmd_list(args):
    vacancies = load_vacancies(
        status=args.status,
        light=True,
        limit=args.limit,
        include_candidate_companies=args.include_candidates,
    )

    rows = []
    for uid, v in vacancies.items():
        score = v.get("llm_score")
        if args.min_score is not None and (score is None or score < args.min_score):
            continue
        if args.tier and v.get("tier") != args.tier:
            continue
        if args.org:
            org = (v.get("org") or "").lower()
            if args.org.lower() not in org:
                continue
        rows.append((uid, v))

    if args.sort == "score":
        rows.sort(key=lambda r: (r[1].get("llm_score") or 0), reverse=True)
    elif args.sort == "recent":
        rows.sort(key=lambda r: r[1].get("created_at") or "", reverse=True)
    elif args.sort == "company":
        rows.sort(key=lambda r: (r[1].get("org") or "").lower())

    if not rows:
        print("Ничего не найдено.")
        return

    width = _term_width()
    company_w = 18
    title_w = max(20, width - 8 - 1 - company_w - 1 - 4 - 1 - 10 - 1 - 14 - 5)

    header = f"{'ID':<8} {'Компания':<{company_w}} {'Должность':<{title_w}} {'Балл':>4} {'Статус':<10} {'Локация':<14}"
    print(_ansi("1", header))
    print("─" * min(width, len(header) + 2))

    for uid, v in rows:
        company = (v.get("org") or "—")[:company_w]
        title = (v.get("title") or "—")[:title_w]
        score_s = _color_score(v.get("llm_score"))
        status_s = _color_status(v.get("status") or "unseen")
        loc = _location_brief(v.get("locations"))[:14]
        print(f"{_short_id(uid):<8} {company:<{company_w}} {title:<{title_w}} {score_s} {status_s:<19} {loc:<14}")

    print()
    print(f"Всего: {len(rows)}")


def cmd_show(args):
    vacancies = load_vacancies(
        light=False,
        include_candidate_companies=True,
        include_inactive_companies=True,
    )
    uid = _resolve_uid(args.id, vacancies)
    if not uid:
        print(f"Не найдено: {args.id}")
        sys.exit(1)
    v = vacancies[uid]
    width = min(_term_width(), 100)

    print(_ansi("1", f"{v.get('org', '—')} — {v.get('title', '—')}"))
    print(f"ID: {uid}   Балл: {v.get('llm_score') or '—'}   Статус: {_color_status(v.get('status') or 'unseen')}")
    if v.get("compensation"):
        print(f"Компенсация: {v['compensation']}")
    if v.get("deadline"):
        print(f"Дедлайн: {v['deadline']}")
    locs = v.get("locations") or []
    if locs:
        print("Локации:")
        for loc in locs:
            url = loc.get("url") or ""
            bits = [loc.get("work_mode") or "", loc.get("city") or loc.get("country") or "", loc.get("compensation") or ""]
            print(f"  - {' / '.join(b for b in bits if b)}  {url}")
    if v.get("llm_summary"):
        print()
        print(_ansi("1", "Summary:"))
        print(textwrap.fill(v["llm_summary"], width=width))
    if v.get("llm_reasoning"):
        print()
        print(_ansi("1", "Почему этот балл:"))
        print(textwrap.fill(v["llm_reasoning"], width=width))
    if v.get("llm_hard_requirements"):
        print()
        print(_ansi("1", "Жёсткие требования:"))
        for r in v["llm_hard_requirements"]:
            print(f"  • {r}")
    if args.full and v.get("full_description"):
        print()
        print(_ansi("1", "Описание:"))
        print(textwrap.fill(v["full_description"], width=width))


def cmd_mark(args):
    if args.status not in VALID_STATUSES:
        print(f"Неверный статус: {args.status}. Допустимые: {', '.join(sorted(VALID_STATUSES))}")
        sys.exit(1)
    vacancies = load_vacancies(
        light=True,
        include_candidate_companies=True,
        include_inactive_companies=True,
    )
    uid = _resolve_uid(args.id, vacancies)
    if not uid:
        print(f"Не найдено: {args.id}")
        sys.exit(1)
    v = vacancies[uid]
    update_vacancy_status(uid, args.status)
    get_conn().commit()
    print(f"ok: {v.get('org', '—')} — {v.get('title', '—')[:50]} → {args.status}")


def cmd_open(args):
    vacancies = load_vacancies(
        light=False,
        include_candidate_companies=True,
        include_inactive_companies=True,
    )
    uid = _resolve_uid(args.id, vacancies)
    if not uid:
        print(f"Не найдено: {args.id}")
        sys.exit(1)
    v = vacancies[uid]
    locs = v.get("locations") or []
    url = next((loc.get("url") for loc in locs if loc.get("url")), None)
    if not url:
        url = v.get("org_url")
    if not url:
        print("Нет ссылки.")
        sys.exit(1)
    print(f"Открываю: {url}")
    if sys.platform == "darwin":
        subprocess.run(["open", url])
    elif os.name == "nt":
        os.startfile(url)
    else:
        subprocess.run(["xdg-open", url])


def cmd_companies(args):
    from database_supabase import get_conn as gc
    conn = gc()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.canonical_name, c.tier, c.alignment_score, c.status,
               COUNT(v.id) FILTER (WHERE v.status != 'unseen') AS triaged,
               COUNT(v.id) AS total
        FROM company c
        LEFT JOIN vacancy v ON v.company_id = c.id
        WHERE (%s IS NULL OR c.status = %s)
        GROUP BY c.id, c.canonical_name, c.tier, c.alignment_score, c.status
        ORDER BY c.alignment_score DESC NULLS LAST, c.canonical_name
        LIMIT %s
    """, (args.status, args.status, args.limit))
    rows = cur.fetchall()
    cur.close()

    if not rows:
        print("Ничего не найдено.")
        return

    print(_ansi("1", f"{'Компания':<32} {'Tier':<5} {'Align':>5}  {'Статус':<10} {'Триаж':>10}"))
    print("─" * 80)
    for name, tier, align, status, triaged, total in rows:
        align_s = f"{align:>3}" if align is not None else "  —"
        triaged_s = f"{triaged}/{total}"
        print(f"{(name or '')[:32]:<32} {tier or '—':<5} {align_s}  {_color_status(status):<19} {triaged_s:>10}")
    print()
    print(f"Всего: {len(rows)}")


def main():
    parser = argparse.ArgumentParser(prog="vac", description="KISS-триаж вакансий из терминала.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Список вакансий с фильтрами.")
    p_list.add_argument("--status", help="Фильтр по статусу: unseen, liked, passed, to_apply…")
    p_list.add_argument("--min-score", type=int, help="Минимальный балл LLM.")
    p_list.add_argument("--tier", help="Фильтр по уровню компании (S/A/B/C).")
    p_list.add_argument("--org", help="Фильтр по подстроке имени компании.")
    p_list.add_argument("--limit", type=int, help="Сколько строк показать.")
    p_list.add_argument("--sort", choices=["score", "recent", "company"], default="score")
    p_list.add_argument("--include-candidates", action="store_true", help="Показать вакансии не-approved компаний.")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Детали одной вакансии.")
    p_show.add_argument("id", help="Префикс UUID (минимум 4 символа).")
    p_show.add_argument("--full", action="store_true", help="Показать full_description.")
    p_show.set_defaults(func=cmd_show)

    p_mark = sub.add_parser("mark", help="Поменять статус вакансии.")
    p_mark.add_argument("id", help="Префикс UUID.")
    p_mark.add_argument("status", help="Новый статус (liked, passed, to_apply, applied, …)")
    p_mark.set_defaults(func=cmd_mark)

    p_open = sub.add_parser("open", help="Открыть ссылку на вакансию в браузере.")
    p_open.add_argument("id", help="Префикс UUID.")
    p_open.set_defaults(func=cmd_open)

    p_co = sub.add_parser("companies", help="Список компаний.")
    p_co.add_argument("--status", help="Фильтр по статусу: active / candidate / inactive.")
    p_co.add_argument("--limit", type=int, default=50)
    p_co.set_defaults(func=cmd_companies)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

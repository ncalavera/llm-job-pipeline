"""Dashboard UI translations — bundled, owner-agnostic.

The generated dashboard chrome (tab labels, basket chips, search placeholders,
filter labels, table headers, buttons, empty states) is keyed by stable
identifiers. The generator picks one language and bakes that language's string
map into ``public/data.js`` (``config.i18n``); the frontend applies it to any
element carrying a ``data-i18n*`` attribute.

Adding a language = add one entry to ``STRINGS`` with the same keys as ``en``.
``"en"`` is the source of truth for the key set and the fallback for any missing
key. Nothing here is personal: these are generic UI labels usable by any fork.
"""

from __future__ import annotations

# Canonical key set lives in "en". Every other language SHOULD provide the same
# keys; ``strings()`` falls back to the English value for any key a translation
# omits, so a partial translation degrades gracefully (mixed-language, never a
# missing label).
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # Document
        "html_lang": "en",
        "page_title": "Job Vacancy Dashboard",
        # Top-nav tabs
        "tab_today": "Today",
        "tab_companies": "Companies",
        "tab_vacancies": "Vacancies",
        "tab_triage": "Triage",
        "tab_geo": "Geo",
        "tab_archive": "Archive",
        "tab_boards": "Boards",
        "updated_prefix": "Updated:",
        # Applyable-roles count (U6)
        "col_applyable": "Applyable / Total",
        "badge_applyable": "applyable",
        "badge_total": "total",
        # Catalog score floor + freshness (U7/U8)
        "catalog_show_all": "Show all",
        "catalog_show_top": "Top only",
        "freshness_fresh": "fresh, open",
        "freshness_stale": "stale, likely closed",
        "freshness_fresh_hint": "the source confirmed this role recently",
        "freshness_stale_hint": "based on the last time the source confirmed the role; direct ATS is exact, aggregators approximate",
        # Triage "Expired" column staleness line
        "triage_stale_seen": "not seen at source for {n} days",
        # Today cockpit (U10/U11)
        "today_expiring": "Expiring, needs a decision",
        "today_ready": "Ready to send",
        "today_new": "New 70+ since last visit",
        "today_none_action": "nothing needs action",
        "today_none_ready": "nothing ready to send",
        "today_none_new": "nothing new",
        "today_protected": "protected, decide",
        "today_deadline_in": "deadline in",
        # Today inline action buttons
        "today_act_apply": "Apply",
        "today_act_applied": "Mark applied",
        "today_act_pass": "Pass",
        "today_act_like": "Like",
        "today_sla": "Decision discipline",
        "today_stuck_hint": "stuck (>=",
        "today_stuck_hint_tail": " d):",
        "today_days_stuck": "d without movement",
        "today_nothing_stuck": "nothing stuck",
        "today_leakage": "leaked to archive/passed this week, roles",
        # Boards tab
        "boards_title": "Job boards",
        "boards_sub": "Live status of the aggregators feeding the pipeline.",
        "boards_loading": "Loading board status…",
        "boards_needs_api": "Board status is only available on the live deployment (needs /api).",
        "boards_load_failed": "Could not load board status.",
        "boards_none": "No boards configured.",
        "boards_col_board": "Board",
        "boards_col_strategy": "Source type",
        "boards_col_tier": "Tier",
        "boards_col_last": "Last fetch",
        "boards_col_status": "Status",
        "boards_col_total": "Vacancies",
        "boards_col_recent": "Fresh 14d",
        "boards_status_never": "Never fetched",
        "boards_status_overdue": "Overdue (TTL {n}d)",
        "boards_status_ok": "Fresh",
        # Catalog loader
        "loading_vacancies": "Sorting vacancies…",
        # Basket chips
        "basket_liked": "Liked",
        "basket_unreviewed": "Unreviewed",
        "basket_passed": "Passed",
        # Catalog filters
        "search_vacancies_ph": "Search by title, organization, location...",
        "all_companies": "All companies",
        "loc_europe": "Europe",
        "loc_us": "US",
        "loc_remote": "Remote",
        "loc_other": "Other",
        "sort_score": "Score",
        # Companies sub-tabs
        "subtab_approved": "Approved",
        "subtab_pending": "Pending Review",
        "subtab_archived": "Archived",
        # Companies filters
        "search_companies_ph": "Search by company, description, location...",
        "all_tiers": "All tiers",
        "tier_s": "S — Strategic",
        "tier_a": "A — Strong Fit",
        "tier_b": "B — Monitor",
        "tier_c": "C — Low Priority",
        "tier_unscored": "— Unscored",
        "csort_applyable": "Applyable",
        "csort_liked": "Liked",
        "csort_score": "Score",
        "csort_interest": "Interest",
        # Companies stat cards (dynamic)
        "stat_total": "Total",
        "stat_with_new": "With new",
        "stat_needs_attention": "Needs attention",
        "stat_pending": "Pending",
        "stat_enriched": "Enriched",
        "stat_rejected": "Rejected",
        "stat_unreviewed_suffix": "unreviewed",
        "stat_stale_suffix": "stale",
        "stat_errors_suffix": "errors",
        # Companies table headers (dynamic)
        "col_company": "Company",
        "col_tier": "Tier",
        "col_fit": "Fit",
        "col_vacancies": "Vacancies",
        "col_liked": "Liked",
        "col_new": "New",
        "col_freshness": "Freshness",
        "col_location": "Location",
        "col_monitoring": "Monitoring",
        "col_category": "Category",
        "col_source": "Source",
        "col_review_hdr": "Review",
        "col_reason": "Reason",
        # Monitoring status cell + chips (dynamic)
        "mon_error": "Fetch error",
        "mon_no_data": "No data",
        "mon_manual": "Manual",
        "mon_no_source": "No source",
        "mon_never": "Never run",
        "mon_working": "Working",
        "mon_days_ago": "{n}d ago",
        "monchip_error": "Errors",
        "monchip_stale": "Stale",
        "monchip_never": "Never run",
        "monchip_nosource": "No source",
        "monchip_nodata": "No data",
        "monchip_manual": "Manual",
        "monchip_ok": "OK",
        # Company review buttons (dynamic)
        "btn_review": "Review",
        "btn_approve": "Approve",
        "btn_reject": "Reject",
        # Catalog (dynamic)
        "catalog_no_match": "Nothing matches the filters",
        "catalog_empty": "No vacancies yet. Fetch some first.",
        "catalog_basket_empty": "no vacancies",
        "catalog_details": "Details",
        "catalog_collapse": "Collapse",
        "region_europe": "Europe",
        "region_americas": "Americas",
        "region_remote": "Remote",
        "region_asia": "Asia",
        "region_africa": "Africa",
        # Triage / pipeline funnel (dynamic)
        "funnel_in_database": "In database",
        "funnel_liked": "Liked",
        "funnel_triaged": "Triaged",
        "funnel_in_progress": "In progress",
        "funnel_applied": "Applied",
        "funnel_passed": "Passed",
        # Geo / stats table (dynamic)
        "geo_city": "City",
        "geo_country": "Country",
        "geo_count": "Count",
        "geo_liked": "Liked",
        "geo_mean_score": "Mean score",
        "geo_remote_unknown": "Remote / Unknown",
        # Archive (dynamic + static)
        "archive_title": "Vacancy archive",
        "archive_sub": "Vacancies archived from earlier runs. View only.",
        "archive_no_match": "Nothing matches the filters",
        "archive_empty": "The archive is empty",
        # Hidden-vacancy notes (candidate companies not yet approved). {orgs}
        # and {vacs} are substituted on the client.
        "companies_pending_hidden": "ℹ️ {orgs} companies here have {vacs} vacancies hidden from your job list — approve a company to surface its roles.",
        "catalog_hidden_pending": "ℹ️ {vacs} vacancies from {orgs} not-yet-approved companies are hidden here — approve the company on the Companies tab to see its roles.",
    },
    # Russian — verbatim from the maintainer's prior dashboard build.
    "ru": {
        "html_lang": "ru",
        "page_title": "Дашборд вакансий",
        "tab_today": "Сегодня",
        "tab_companies": "Компании",
        "tab_vacancies": "Вакансии",
        "tab_triage": "Триаж",
        "tab_geo": "Гео",
        "tab_archive": "Архив",
        "tab_boards": "Доски",
        "updated_prefix": "Обновлено:",
        # Applyable-roles count (U6)
        "col_applyable": "Годные / Всего",
        "badge_applyable": "годных",
        "badge_total": "всего",
        # Catalog score floor + freshness (U7/U8)
        "catalog_show_all": "Показать все",
        "catalog_show_top": "Только годные",
        "freshness_fresh": "свежая, открыта",
        "freshness_stale": "давно не видели, вероятно закрыта",
        "freshness_fresh_hint": "источник подтверждал роль недавно",
        "freshness_stale_hint": "оценка по дате последнего показа в источнике; прямые ATS точны, агрегаторы приблизительны",
        # Triage "Expired" column staleness line
        "triage_stale_seen": "нет в источнике уже {n} дн.",
        # Today cockpit (U10/U11)
        "today_expiring": "Истекает, нужно решение",
        "today_ready": "Готово к отправке",
        "today_new": "Новое 70+ с прошлого захода",
        "today_none_action": "ничего не требует действий",
        "today_none_ready": "нет готовых к отправке",
        "today_none_new": "нового нет",
        "today_protected": "защищённая, решить",
        "today_deadline_in": "дедлайн через",
        # Today inline action buttons
        "today_act_apply": "Откликнуться",
        "today_act_applied": "Отправлено",
        "today_act_pass": "Отказ",
        "today_act_like": "В избранное",
        "today_sla": "Дисциплина решений",
        "today_stuck_hint": "застряло (≥",
        "today_stuck_hint_tail": " дн.):",
        "today_days_stuck": "дн. без движения",
        "today_nothing_stuck": "ничего не застряло",
        "today_leakage": "за неделю ушло в архив/отказ ролей",
        # Boards tab
        "boards_title": "Доски вакансий",
        "boards_sub": "Живой статус агрегаторов, которые питают конвейер.",
        "boards_loading": "Загружаю статус досок…",
        "boards_needs_api": "Статус досок доступен только на живом развёртывании (нужен /api).",
        "boards_load_failed": "Не удалось загрузить статус досок.",
        "boards_none": "Нет настроенных досок.",
        "boards_col_board": "Доска",
        "boards_col_strategy": "Тип сбора",
        "boards_col_tier": "Тир",
        "boards_col_last": "Последний сбор",
        "boards_col_status": "Статус",
        "boards_col_total": "Вакансий",
        "boards_col_recent": "Свежих 14д",
        "boards_status_never": "Ни разу не собиралась",
        "boards_status_overdue": "Просрочена (TTL {n}д)",
        "boards_status_ok": "Свежая",
        "loading_vacancies": "Разбираем вакансии…",
        "basket_liked": "Выбранные",
        "basket_unreviewed": "Неразобранные",
        "basket_passed": "Откинутые",
        "search_vacancies_ph": "Поиск по названию, организации, локации...",
        "all_companies": "Все компании",
        "loc_europe": "Европа",
        "loc_us": "US",
        "loc_remote": "Remote",
        "loc_other": "Другое",
        "sort_score": "Скор",
        "subtab_approved": "Одобренные",
        "subtab_pending": "На ревью",
        "subtab_archived": "Архив",
        "search_companies_ph": "Поиск по компании, описанию, локации...",
        "all_tiers": "Все тиры",
        "tier_s": "S — Strategic",
        "tier_a": "A — Strong Fit",
        "tier_b": "B — Monitor",
        "tier_c": "C — Low Priority",
        "tier_unscored": "— Без скора",
        "csort_applyable": "Годные",
        "csort_liked": "Выбранные",
        "csort_score": "Скор",
        "csort_interest": "Интерес",
        # Companies stat cards
        "stat_total": "Всего",
        "stat_with_new": "С новыми",
        "stat_needs_attention": "Нужно внимание",
        "stat_pending": "На ревью",
        "stat_enriched": "Обогащено",
        "stat_rejected": "Отклонено",
        "stat_unreviewed_suffix": "неразобрано",
        "stat_stale_suffix": "устарело",
        "stat_errors_suffix": "ошибок",
        # Companies table headers
        "col_company": "Компания",
        "col_tier": "Тир",
        "col_fit": "Фит",
        "col_vacancies": "Вакансии",
        "col_liked": "Выбрано",
        "col_new": "Новые",
        "col_freshness": "Свежесть",
        "col_location": "Локация",
        "col_monitoring": "Мониторинг",
        "col_category": "Категория",
        "col_source": "Источник",
        "col_review_hdr": "Ревью",
        "col_reason": "Причина",
        # Monitoring status cell + chips
        "mon_error": "Ошибка fetch",
        "mon_no_data": "Нет данных",
        "mon_manual": "Ручной",
        "mon_no_source": "Нет источника",
        "mon_never": "Не запускали",
        "mon_working": "Работает",
        "mon_days_ago": "{n}д назад",
        "monchip_error": "Ошибки",
        "monchip_stale": "Устарело",
        "monchip_never": "Не запускали",
        "monchip_nosource": "Нет источника",
        "monchip_nodata": "Нет данных",
        "monchip_manual": "Ручной",
        "monchip_ok": "Ок",
        # Company review buttons
        "btn_review": "Ревью",
        "btn_approve": "Одобрить",
        "btn_reject": "Отклонить",
        # Catalog
        "catalog_no_match": "Ничего не найдено по фильтрам",
        "catalog_empty": "Вакансий пока нет. Сначала соберите их.",
        "catalog_basket_empty": "вакансий нет",
        "catalog_details": "Подробнее",
        "catalog_collapse": "Свернуть",
        "region_europe": "Европа",
        "region_americas": "Америка",
        "region_remote": "Remote",
        "region_asia": "Азия",
        "region_africa": "Африка",
        # Triage / pipeline funnel
        "funnel_in_database": "В базе",
        "funnel_liked": "Лайкнуто",
        "funnel_triaged": "Прошли триаж",
        "funnel_in_progress": "В работе",
        "funnel_applied": "Подано",
        "funnel_passed": "Откинутые",
        # Geo / stats table
        "geo_city": "Город",
        "geo_country": "Страна",
        "geo_count": "Количество",
        "geo_liked": "Выбрано",
        "geo_mean_score": "Средний скор",
        "geo_remote_unknown": "Remote / Неизвестно",
        # Archive
        "archive_title": "Архив вакансий",
        "archive_sub": "Старые вакансии из прошлых запусков. Только просмотр.",
        "archive_no_match": "Ничего не найдено по фильтрам",
        "archive_empty": "Архив пуст",
        "companies_pending_hidden": "ℹ️ {orgs} компаний здесь держат {vacs} вакансий, скрытых из вашего списка — одобрите компанию, чтобы её вакансии появились.",
        "catalog_hidden_pending": "ℹ️ {vacs} вакансий из {orgs} ещё не одобренных компаний скрыты — одобрите компанию на вкладке «Компании», чтобы увидеть их.",
    },
}

DEFAULT_LANGUAGE = "en"


def available_languages() -> list[str]:
    """Languages with a bundled string map."""
    return sorted(STRINGS.keys())


def strings(language: str) -> dict[str, str]:
    """Return the full string map for ``language``.

    Unknown language → English. Missing keys in a known language are filled from
    English so the returned map always has the canonical key set.
    """
    base = dict(STRINGS[DEFAULT_LANGUAGE])
    chosen = STRINGS.get((language or "").strip().lower())
    if chosen:
        base.update(chosen)
    return base

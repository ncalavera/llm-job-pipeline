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
        "tab_companies": "Companies",
        "tab_vacancies": "Vacancies",
        "tab_triage": "Triage",
        "tab_geo": "Geo",
        "tab_archive": "Archive",
        "updated_prefix": "Updated:",
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
        "csort_liked": "Liked",
        "csort_score": "Score",
        "csort_interest": "Interest",
        # Companies table headers / cells (dynamic)
        "col_tier": "Tier",
        "col_liked": "Liked",
        "col_monitoring": "Monitoring",
        "company_no_data": "No data",
        "company_no_source": "No source",
        # Company review buttons (dynamic)
        "btn_review": "Review",
        "btn_approve": "Approve",
        "btn_reject": "Reject",
        # Archive
        "archive_title": "Vacancy archive",
        "archive_sub": "Vacancies archived from earlier runs. View only.",
    },
    # Russian — verbatim from the maintainer's prior dashboard build.
    "ru": {
        "html_lang": "ru",
        "page_title": "Дашборд вакансий",
        "tab_companies": "Компании",
        "tab_vacancies": "Вакансии",
        "tab_triage": "Триаж",
        "tab_geo": "Гео",
        "tab_archive": "Архив",
        "updated_prefix": "Обновлено:",
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
        "subtab_approved": "Approved",
        "subtab_pending": "Pending Review",
        "subtab_archived": "Archived",
        "search_companies_ph": "Поиск по компании, описанию, локации...",
        "all_tiers": "Все тиры",
        "tier_s": "S — Strategic",
        "tier_a": "A — Strong Fit",
        "tier_b": "B — Monitor",
        "tier_c": "C — Low Priority",
        "tier_unscored": "— Без скора",
        "csort_liked": "Выбранные",
        "csort_score": "Скор",
        "csort_interest": "Интерес",
        "col_tier": "Тир",
        "col_liked": "Выбрано",
        "col_monitoring": "Мониторинг",
        "company_no_data": "Нет данных",
        "company_no_source": "Нет источника",
        "btn_review": "Ревью",
        "btn_approve": "Одобрить",
        "btn_reject": "Отклонить",
        "archive_title": "Архив вакансий",
        "archive_sub": "Старые вакансии из прошлых запусков. Только просмотр.",
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

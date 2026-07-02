"""The ONE product language — chosen once, applied everywhere.

The user picks a language at onboarding (the questionnaire in ``docs/index.html``)
and it lands in the profile's ``## OUTPUT_LANGUAGE`` section. That single choice is
the product language: it drives the agent's replies in the runbooks, the run
banner + summary printed by ``run_daily.py``, the Telegram digest, and the
dashboard's default language. Changing it is one edit (``/jobs-profile`` → the
same section); everything follows.

Single source, one resolver, table-driven — so adding a language is data, not
code scattered across ten call sites:

  * ``LANG_ALIASES`` maps whatever a human wrote ("English", "en", "Russian",
    "ru", "Русский") to a canonical two-letter code.
  * a code only counts as a *product* language when ``scripts/i18n.py`` bundles a
    string table for it (``i18n.available_languages()``). A profile that names a
    language with no bundled table (e.g. "French") degrades to English for the
    UI chrome, while the scoring prompt still writes its text fields in that
    language (the prompt substitutes the raw ``## OUTPUT_LANGUAGE`` value — this
    module never touches that).

Resolution order (first hit wins):

  1. ``PRODUCT_LANGUAGE`` env var — an explicit override (tests, power users).
  2. the profile's ``## OUTPUT_LANGUAGE`` section — the single human source.
  3. ``[dashboard] language`` in ``config/defaults.toml`` — legacy fallback for a
     fork that set the old dashboard-only knob and never touched the profile.
  4. ``en`` — the neutral default.

Never raises: a missing profile / section / settings loader degrades to English
so every caller gets a usable language.
"""

from __future__ import annotations

import os

DEFAULT_LANGUAGE = "en"

# What a human might write in ## OUTPUT_LANGUAGE → canonical code. Extend this
# (and add a table to scripts/i18n.py) to ship a new language. Keys are matched
# case-insensitively after stripping.
LANG_ALIASES: dict[str, str] = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "русский": "ru",
}

# Human label per code, for the dashboard Settings row (shows what's in effect).
LANG_LABELS: dict[str, str] = {
    "en": "English",
    "ru": "Русский",
}

# Values that mean "the user left this field empty" (matches hard_filters.py).
_EMPTY_TOKENS = {"", "(none)", "none", "-", "n/a", "na"}


def _bundled_languages() -> set[str]:
    """Codes that have a bundled dashboard string table (so chrome can render)."""
    try:
        import i18n

        return set(i18n.available_languages())
    except Exception:
        return {DEFAULT_LANGUAGE}


def _normalize(raw: str | None) -> str:
    """Map a raw language token to a bundled code, or ``""`` if unresolvable.

    Only returns a code that ``scripts/i18n.py`` can actually render; an alias
    for an unbundled language (or unknown text) yields ``""`` so the caller
    falls through to the next source.
    """
    token = (raw or "").strip().lower()
    if token in _EMPTY_TOKENS:
        return ""
    code = LANG_ALIASES.get(token, "")
    return code if code in _bundled_languages() else ""


def _profile_language() -> str:
    """Normalized product language from the profile's ## OUTPUT_LANGUAGE, else "".

    Reuses the single profile reader so it sees exactly the same file as the
    scoring prompts and the hard filters. Never raises.
    """
    try:
        from prompts import _load_user_profile

        sections = _load_user_profile()
    except Exception:
        return ""
    return _normalize(sections.get("OUTPUT_LANGUAGE"))


def _settings_dashboard_language() -> str:
    """Normalized language from the legacy ``[dashboard] language`` knob, else "".

    A fork that predates this feature may have set the language only in
    ``config/defaults.toml``; honour it when the profile says nothing.
    """
    try:
        import settings

        return _normalize(str(settings.dashboard().get("language", "")))
    except Exception:
        return ""


def resolve() -> str:
    """The one resolved product-language code (e.g. ``"en"``, ``"ru"``).

    See the module docstring for the resolution order. Always returns a bundled
    code; ``en`` when nothing else resolves.
    """
    env = _normalize(os.environ.get("PRODUCT_LANGUAGE"))
    if env:
        return env
    prof = _profile_language()
    if prof:
        return prof
    legacy = _settings_dashboard_language()
    if legacy:
        return legacy
    return DEFAULT_LANGUAGE


def language_label(code: str | None = None) -> str:
    """Human label for a code (``"en"`` → ``"English"``), for display only."""
    c = (code or resolve()).strip().lower()
    return LANG_LABELS.get(c, c or DEFAULT_LANGUAGE)


def strings(lang: str | None = None) -> dict[str, str]:
    """The full string map for ``lang`` (defaults to the resolved language).

    Thin passthrough to ``i18n.strings`` so callers have one import. Falls back
    to an empty map if i18n is unavailable (bare host) — ``t()`` then returns the
    key/fallback unchanged.
    """
    code = (lang or resolve()).strip().lower()
    try:
        import i18n

        return i18n.strings(code)
    except Exception:
        return {}


def t(key: str, /, lang: str | None = None, **fmt) -> str:
    """Translate ``key`` into the (resolved or given) product language.

    Unknown keys return the key itself, so a missing translation is visible but
    never crashes. ``**fmt`` are substituted with ``str.format`` (e.g.
    ``t("banner_active", n=5, cap=200)``); a formatting mismatch degrades to the
    unformatted string rather than raising.
    """
    table = strings(lang)
    value = table.get(key, key)
    if fmt:
        try:
            return value.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return value
    return value

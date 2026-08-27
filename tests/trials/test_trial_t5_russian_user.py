"""Trial T5 — "Russian user".

A user who picks Russian at onboarding must get the whole product in Russian from
that single choice: the agent's replies, the run banner + summary, the Telegram
digest, and the dashboard chrome. One profile key drives all of them.

The resolver order and per-surface translation are unit-tested in
``test_product_language.py``; the public English shell staying Cyrillic-free is
in ``test_dashboard_no_cyrillic.py``. This trial is the persona integration: one
``## OUTPUT_LANGUAGE`` choice flips every named surface at once, and the Russian
product has no missing translation that would silently fall back to English.
"""

from __future__ import annotations

import trial_harness as h

RU_PROFILE = "## OUTPUT_LANGUAGE\n\nRussian\n\n## TARGET_ROLES\n\n- Product Designer, UX Designer\n"

# Load-bearing surfaces that MUST read as Russian (not an accidental English copy).
_MUST_TRANSLATE = ("summary_done", "banner_title", "digest_run_header", "tab_today")


def test_one_choice_switches_all_surfaces_to_russian(monkeypatch, tmp_path):
    profile = tmp_path / "profile_ru.md"
    profile.write_text(RU_PROFILE, encoding="utf-8")
    h.swap_profile(monkeypatch, str(profile))

    import i18n
    import product_language as pl

    # The one resolved product language — drives the agent's replies in the runbooks.
    assert pl.resolve() == "ru"

    # Run banner + summary (run_daily.py reads these through product_language.t).
    assert pl.t("banner_title") == "/jobs-new — объём на сегодня"
    assert pl.t("summary_done") == "✓ /jobs-new завершён"

    # Telegram digest copy.
    assert "Ночной прогон" in pl.t("digest_run_header")

    # Dashboard chrome (baked into data.js from the same table).
    assert i18n.strings("ru")["tab_today"] == "Сегодня"


def test_russian_product_has_no_missing_translation(monkeypatch, tmp_path):
    """Every English key exists in Russian, so no surface silently reverts to English."""
    import i18n

    en_keys = set(i18n.STRINGS["en"])
    ru_keys = set(i18n.STRINGS["ru"])
    missing = en_keys - ru_keys
    assert not missing, f"Russian product is missing translations for: {sorted(missing)}"

    # The load-bearing surfaces are genuinely translated, not English left in place.
    for key in _MUST_TRANSLATE:
        assert i18n.STRINGS["ru"][key] != i18n.STRINGS["en"][key], (
            f"{key} not translated to Russian"
        )

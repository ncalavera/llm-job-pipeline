// =============================================================================
// i18n.js — Apply the baked language strings to the shell.
//
// The generator bakes the chosen language's string map (config.i18n) into
// data.js. This module reads it and rewrites the static index.html chrome at
// load time:
//   - [data-i18n="key"]      → element.textContent     = T(key)
//   - [data-i18n-ph="key"]   → element.placeholder     = T(key)
// English text in index.html stays as the in-markup fallback, so a fork with no
// translation renders correctly with zero baked config.
// =============================================================================

import { config } from "./state.js";

const LANG_STORAGE_KEY = "dashboard_lang";
const ALL = (config && config.i18n_all) || null;

/** Languages the dashboard can switch between (from the baked i18n_all). */
export function availableLanguages() {
  return ALL ? Object.keys(ALL) : [(config && config.language) || "en"];
}

/**
 * Pure resolver for the active UI language. Order:
 *   1. an explicit user toggle saved in localStorage (only if we bundle it),
 *   2. else the server-baked default `config.language` — which the generator
 *      sets from the profile's product language (## OUTPUT_LANGUAGE),
 *   3. else "en".
 * Kept side-effect-free so it unit-tests without DOM/localStorage.
 */
export function pickLanguage(saved, configLang, all) {
  if (saved && all && all[saved]) return saved;
  return configLang || "en";
}

/** The active UI language: user's saved toggle, else the profile-baked default. */
export function getLanguage() {
  var saved = null;
  try {
    saved = localStorage.getItem(LANG_STORAGE_KEY);
  } catch (e) {
    saved = null;
  }
  return pickLanguage(saved, config && config.language, ALL);
}

/** Intl locale for the active dashboard language (date/number formatting). */
export function dateLocale() {
  return getLanguage() === "ru" ? "ru-RU" : "en-US";
}

/** Persist a language choice and reload so every view re-renders in it. */
export function setLanguage(lang) {
  if (lang === getLanguage()) return;
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch (e) {
    /* ignore — the reload below still applies it for this load */
  }
  location.reload();
}

const STRINGS = (ALL && ALL[getLanguage()]) || (config && config.i18n) || {};

// Strings that ship with the browser code, not with the baked snapshot: the
// Screen view (bulk screening inbox) lives on the dashboard line, whose
// snapshot is written by a different branch. A baked string still wins.
const LOCAL = {
  en: {
    screen_tab: "Screen",
    screen_title: "Make one decision about several roles.",
    screen_list_to_screen: "To screen",
    screen_list_kept: "Kept",
    screen_list_aside: "Put aside",
    screen_group_language: "A language requirement",
    screen_group_onsite: "Onsite or location constraint",
    screen_group_seniority: "Seniority stated",
    screen_group_unclear: "Eligibility unclear",
    screen_group_all: "All remaining roles",
    screen_group_hint: "Groups filter the To screen list only.",
    screen_required: "Required",
    screen_preferred: "Preferred",
    screen_unknown: "Unknown",
    screen_evidence: "Read posting evidence",
    screen_no_quote: "no quote",
    screen_profile_notes: "Compared with your profile",
    screen_empty: "No roles left in this list.",
    screen_processing: "Not prepared yet: {unprepared} · Failed: {failed}",
    screen_selected: "{n} selected",
    screen_select_all: "Select all",
    screen_clear: "Clear selection",
    screen_keep: "Keep",
    screen_put_aside: "Put aside",
    screen_undo: "Undo",
    screen_saved: "{n} of {m} saved",
    screen_undone: "{n} of {m} restored",
    screen_loading: "Loading statuses…",
    screen_open: "Open",
  },
  ru: {
    screen_tab: "Отбор",
    screen_title: "Одно решение сразу о нескольких вакансиях.",
    screen_list_to_screen: "На отбор",
    screen_list_kept: "Оставлены",
    screen_list_aside: "Отложены",
    screen_group_language: "Требование к языку",
    screen_group_onsite: "Офис или ограничение по месту",
    screen_group_seniority: "Указан уровень",
    screen_group_unclear: "Непонятно, подхожу ли",
    screen_group_all: "Все оставшиеся вакансии",
    screen_group_hint: "Группы фильтруют только список «На отбор».",
    screen_required: "Обязательно",
    screen_preferred: "Желательно",
    screen_unknown: "Неясно",
    screen_evidence: "Цитаты из вакансии",
    screen_no_quote: "нет цитаты",
    screen_profile_notes: "Сравнение с профилем",
    screen_empty: "В этом списке вакансий не осталось.",
    screen_processing: "Ещё не подготовлено: {unprepared} · Ошибка: {failed}",
    screen_selected: "Выбрано: {n}",
    screen_select_all: "Выбрать все",
    screen_clear: "Снять выбор",
    screen_keep: "Оставить",
    screen_put_aside: "Отложить",
    screen_undo: "Отменить",
    screen_saved: "Сохранено {n} из {m}",
    screen_undone: "Восстановлено {n} из {m}",
    screen_loading: "Загружаем статусы…",
    screen_open: "Открыть",
  },
};
const LOCAL_STRINGS = LOCAL[getLanguage()] || LOCAL.en;

function has(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key);
}

/** Translate a stable key. Returns the baked string, else the fallback. */
export function T(key, fallback) {
  if (has(STRINGS, key)) return STRINGS[key];
  if (has(LOCAL_STRINGS, key)) return LOCAL_STRINGS[key];
  return fallback !== undefined ? fallback : key;
}

/** Apply all [data-i18n*] translations to the document. */
export function applyI18n() {
  // Document language attribute (affects screen readers, hyphenation).
  if (STRINGS.html_lang) document.documentElement.lang = STRINGS.html_lang;
  if (STRINGS.page_title) document.title = STRINGS.page_title;

  // Text content.
  document.querySelectorAll("[data-i18n]").forEach(function (el) {
    var key = el.getAttribute("data-i18n");
    var text = T(key, null);
    if (text !== null) el.textContent = text;
  });

  // Placeholders (search inputs).
  document.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
    var key = el.getAttribute("data-i18n-ph");
    if (Object.prototype.hasOwnProperty.call(STRINGS, key)) {
      el.setAttribute("placeholder", STRINGS[key]);
    }
  });
}

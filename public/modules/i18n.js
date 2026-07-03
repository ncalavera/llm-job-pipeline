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

/** Translate a stable key. Returns the baked string, else the fallback. */
export function T(key, fallback) {
  if (Object.prototype.hasOwnProperty.call(STRINGS, key)) return STRINGS[key];
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
    if (Object.prototype.hasOwnProperty.call(STRINGS, key)) {
      el.textContent = STRINGS[key];
    }
  });

  // Placeholders (search inputs).
  document.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
    var key = el.getAttribute("data-i18n-ph");
    if (Object.prototype.hasOwnProperty.call(STRINGS, key)) {
      el.setAttribute("placeholder", STRINGS[key]);
    }
  });
}

// =============================================================================
// i18n.js — Apply the baked language strings + illustration pack to the shell.
//
// The generator bakes the chosen language's string map (config.i18n) and the
// illustration pack's image URLs (config.pack_images) into data.js. This module
// reads them and rewrites the static index.html chrome at load time:
//   - [data-i18n="key"]      → element.textContent     = T(key)
//   - [data-i18n-ph="key"]   → element.placeholder     = T(key)
//   - [data-img="slot"]      → image src/data-src swapped to the pack's URL
// English text in index.html stays as the in-markup fallback, so a fork with no
// translation (or the default pack) renders correctly with zero baked config.
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
const PACK_IMAGES = (config && config.pack_images) || {};

/** Translate a stable key. Returns the baked string, else the fallback. */
export function T(key, fallback) {
  if (Object.prototype.hasOwnProperty.call(STRINGS, key)) return STRINGS[key];
  return fallback !== undefined ? fallback : key;
}

/** Apply all [data-i18n*] translations + [data-img] pack swaps to the document. */
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

  // Illustration pack — swap each image's source by its slot. Some images are
  // lazy-loaded via data-src (set on tab activation); rewrite whichever is set.
  document.querySelectorAll("[data-img]").forEach(function (img) {
    var slot = img.getAttribute("data-img");
    if (!Object.prototype.hasOwnProperty.call(PACK_IMAGES, slot)) return;
    var url = PACK_IMAGES[slot];
    if (img.hasAttribute("data-src")) img.setAttribute("data-src", url);
    else img.src = url;
  });
}

// i18n.js — client-side language resolution.
//
// The active UI language is the profile-baked default (config.language, set by
// the generator from the profile's product language) UNLESS the user made an
// explicit toggle saved in localStorage, which wins. pickLanguage is the pure
// core; getLanguage wires it to localStorage + config.
//
// i18n.js imports state.js, which reads window.VACANCY_DATA and touches
// window/location at load — so a minimal browser shell is installed BEFORE the
// dynamic import below.

import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  VACANCY_DATA: {
    config: { language: "ru", i18n_all: { en: { x: "en" }, ru: { x: "ru" } } },
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };

let _store = {};
globalThis.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => {
    _store[k] = String(v);
  },
};

const { pickLanguage, getLanguage } = await import("./i18n.js");

const ALL = { en: {}, ru: {} };

test("pickLanguage: an explicit saved toggle wins when it is a bundled language", () => {
  assert.equal(pickLanguage("ru", "en", ALL), "ru");
});

test("pickLanguage: with no saved toggle, the profile-baked default is used", () => {
  assert.equal(pickLanguage(null, "ru", ALL), "ru");
});

test("pickLanguage: a saved value we do not bundle is ignored — the default wins", () => {
  assert.equal(pickLanguage("xx", "en", ALL), "en");
});

test("pickLanguage: the ultimate fallback is en", () => {
  assert.equal(pickLanguage(null, null, null), "en");
});

test("getLanguage: default comes from config.language (profile-baked)", () => {
  _store = {};
  assert.equal(getLanguage(), "ru");
});

test("getLanguage: an explicit localStorage toggle overrides the default", () => {
  _store = { dashboard_lang: "en" };
  assert.equal(getLanguage(), "en");
});

test("getLanguage: an unknown stored value is ignored, default wins", () => {
  _store = { dashboard_lang: "zz" };
  assert.equal(getLanguage(), "ru");
});

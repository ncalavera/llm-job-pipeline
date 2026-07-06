// nav.js — the six-section routing model. Pure functions, no DOM/state, so
// they unit-test directly (DHA-348).

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SECTIONS,
  VACANCY_VIEWS,
  DEFAULT_VACANCY_VIEW,
  sectionForMode,
  isVacancyView,
  SYNC_STALE_AFTER,
  initialSyncState,
  nextSyncState,
  syncStatusLabelKey,
  FALLBACK_STALE_AFTER_MS,
  fallbackBannerState,
  parseSnapshotStamp,
} from "./nav.js";

test("there are exactly six top-nav sections, in order", () => {
  assert.deepEqual(SECTIONS, [
    "today",
    "vacancies",
    "companies",
    "triage",
    "boards",
    "settings",
  ]);
});

test("every leaf mode maps to its owning section", () => {
  assert.equal(sectionForMode("today"), "today");
  assert.equal(sectionForMode("catalog"), "vacancies");
  assert.equal(sectionForMode("stats"), "vacancies");
  assert.equal(sectionForMode("archive"), "vacancies");
  assert.equal(sectionForMode("companies"), "companies");
  // Triage (DHA-413: the Applications section it used to share a nav slot
  // with was deleted) is its own top-nav section now.
  assert.equal(sectionForMode("pipeline"), "triage");
  assert.equal(sectionForMode("boards"), "boards");
  assert.equal(sectionForMode("settings"), "settings");
});

test("the deep-link detail screens map to their owning parent section (U4)", () => {
  // route.js's route.screen values — the router highlights the owning section
  // through the same function so a cold ?vacancy=/?company= deep link lights
  // up the right sidebar parent (AE6).
  assert.equal(sectionForMode("vacancy"), "vacancies");
  assert.equal(sectionForMode("company"), "companies");
});

test("an unknown mode falls back to the Today section (safe home)", () => {
  assert.equal(sectionForMode("nonsense"), "today");
  assert.equal(sectionForMode(undefined), "today");
});

test("the three vacancy sub-views are recognised; nothing else is", () => {
  assert.deepEqual(VACANCY_VIEWS, ["catalog", "stats", "archive"]);
  for (const v of VACANCY_VIEWS) assert.equal(isVacancyView(v), true);
  assert.equal(isVacancyView("pipeline"), false);
  assert.equal(isVacancyView("today"), false);
  assert.equal(isVacancyView("companies"), false);
  assert.equal(isVacancyView("settings"), false);
});

test("the default vacancy view is one of the vacancy views", () => {
  assert.ok(VACANCY_VIEWS.includes(DEFAULT_VACANCY_VIEW));
});

// --- Sidebar sync-status state machine (DHA-387, U3) ------------------------

test("initial sync state is 'checking' with no failures", () => {
  assert.deepEqual(initialSyncState(), {
    status: "checking",
    consecutiveFailures: 0,
  });
});

test("checking -> ok on the first successful poll", () => {
  const s = nextSyncState(initialSyncState(), "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("a single soft failure while checking doesn't flip the status yet", () => {
  const s = nextSyncState(initialSyncState(), "soft_fail");
  assert.equal(s.status, "checking");
  assert.equal(s.consecutiveFailures, 1);
});

test("soft failures accumulate to 'stale' exactly at SYNC_STALE_AFTER", () => {
  assert.equal(SYNC_STALE_AFTER, 3);
  let s = initialSyncState();
  for (let i = 1; i < SYNC_STALE_AFTER; i++) {
    s = nextSyncState(s, "soft_fail");
    assert.equal(s.status, "checking", `still checking after ${i} blip(s)`);
    assert.equal(s.consecutiveFailures, i);
  }
  s = nextSyncState(s, "soft_fail");
  assert.equal(s.status, "stale");
  assert.equal(s.consecutiveFailures, SYNC_STALE_AFTER);
});

test("a soft failure while ok holds 'ok' until the threshold is crossed", () => {
  let s = nextSyncState(initialSyncState(), "ok"); // ok, 0 failures
  s = nextSyncState(s, "soft_fail");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 1 });
  s = nextSyncState(s, "soft_fail");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 2 });
  s = nextSyncState(s, "soft_fail");
  assert.deepEqual(s, { status: "stale", consecutiveFailures: 3 });
});

test("a hard failure jumps straight to 'error', bypassing the soft-fail counter", () => {
  const s = nextSyncState(
    { status: "ok", consecutiveFailures: 0 },
    "hard_fail",
  );
  assert.deepEqual(s, { status: "error", consecutiveFailures: 0 });
});

test("full cycle: checking -> ok -> stale -> error -> ok", () => {
  let s = initialSyncState();
  s = nextSyncState(s, "ok");
  assert.equal(s.status, "ok");
  for (let i = 0; i < SYNC_STALE_AFTER; i++) s = nextSyncState(s, "soft_fail");
  assert.equal(s.status, "stale");
  s = nextSyncState(s, "hard_fail");
  assert.equal(s.status, "error");
  s = nextSyncState(s, "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("alternate order: ok -> error -> ok (skipping stale entirely)", () => {
  let s = nextSyncState(initialSyncState(), "ok");
  s = nextSyncState(s, "hard_fail");
  assert.equal(s.status, "error");
  s = nextSyncState(s, "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("'ok' recovers straight from 'stale' with the failure count reset", () => {
  let s = initialSyncState();
  for (let i = 0; i < SYNC_STALE_AFTER; i++) s = nextSyncState(s, "soft_fail");
  assert.equal(s.status, "stale");
  s = nextSyncState(s, "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("syncStatusLabelKey maps every known status; unknown falls back to checking", () => {
  assert.equal(syncStatusLabelKey("checking"), "sync_checking");
  assert.equal(syncStatusLabelKey("ok"), "sync_live");
  assert.equal(syncStatusLabelKey("stale"), "sync_stale");
  assert.equal(syncStatusLabelKey("error"), "sync_error");
  assert.equal(syncStatusLabelKey("nonsense"), "sync_checking");
});

// --- Fallback banner (DHA-422) -----------------------------------------------

const NOW = Date.parse("2026-07-06T12:00:00Z");

test("no banner when the source is live, regardless of the stamp", () => {
  assert.deepEqual(fallbackBannerState("live", "2026-07-06T00:00:00Z", NOW), {
    show: false,
    level: "info",
    age: "known",
  });
  assert.deepEqual(fallbackBannerState("live", null, NOW), {
    show: false,
    level: "info",
    age: "known",
  });
});

test("no banner for a hard error either — that's a full-page stop, not a fallback render", () => {
  assert.equal(fallbackBannerState("error", null, NOW).show, false);
});

test("fallback with a fresh stamp (<48h old): info banner, known age", () => {
  const oneHourAgo = new Date(NOW - 60 * 60 * 1000).toISOString();
  assert.deepEqual(fallbackBannerState("fallback", oneHourAgo, NOW), {
    show: true,
    level: "info",
    age: "known",
  });
});

test("fallback exactly at the 48h boundary is still info, not warning", () => {
  const exactly48hAgo = new Date(NOW - FALLBACK_STALE_AFTER_MS).toISOString();
  assert.equal(
    fallbackBannerState("fallback", exactly48hAgo, NOW).level,
    "info",
  );
});

test("fallback with a stamp older than 48h: warning banner, known age", () => {
  const fortyNineHoursAgo = new Date(NOW - 49 * 60 * 60 * 1000).toISOString();
  assert.deepEqual(fallbackBannerState("fallback", fortyNineHoursAgo, NOW), {
    show: true,
    level: "warning",
    age: "known",
  });
});

test("fallback with no stamp at all (old pre-DHA-422 bake): warning banner, unknown age", () => {
  assert.deepEqual(fallbackBannerState("fallback", null, NOW), {
    show: true,
    level: "warning",
    age: "unknown",
  });
  assert.deepEqual(fallbackBannerState("fallback", undefined, NOW), {
    show: true,
    level: "warning",
    age: "unknown",
  });
  assert.deepEqual(fallbackBannerState("fallback", "", NOW), {
    show: true,
    level: "warning",
    age: "unknown",
  });
});

test("fallback with an unparseable stamp is treated the same as no stamp", () => {
  assert.deepEqual(fallbackBannerState("fallback", "not-a-date", NOW), {
    show: true,
    level: "warning",
    age: "unknown",
  });
});

// --- Stamp parsing: naive vs timezone-aware (the generator's real formats) --

test("a NAIVE offset-less stamp (pre-fix bakes) is pinned to UTC, not browser-local", () => {
  // datetime.now().isoformat(timespec="seconds") — no Z, no offset. Browsers
  // would parse this as LOCAL time, skewing the 48h check per viewer; the
  // parser pins it to UTC instead, same instant everywhere.
  assert.equal(
    parseSnapshotStamp("2026-07-06T12:00:00"),
    Date.parse("2026-07-06T12:00:00Z"),
  );
});

test("timezone-aware stamps parse as-written: +00:00 (the generator's new format) and Z", () => {
  // datetime.now(timezone.utc).isoformat(timespec="seconds") ends "+00:00".
  assert.equal(parseSnapshotStamp("2026-07-06T12:00:00+00:00"), NOW);
  assert.equal(parseSnapshotStamp("2026-07-06T12:00:00Z"), NOW);
  // A non-UTC offset is respected, not double-shifted.
  assert.equal(
    parseSnapshotStamp("2026-07-06T14:00:00+02:00"),
    Date.parse("2026-07-06T12:00:00Z"),
  );
});

test("the 48h staleness check is exercised with the real naive format, not just toISOString", () => {
  // 49h before NOW, written the way the pre-fix generator wrote it (naive,
  // seconds precision, no designator). Must read as stale/warning.
  const naive49hAgo = new Date(NOW - 49 * 60 * 60 * 1000)
    .toISOString()
    .replace(/\.\d{3}Z$/, "");
  assert.deepEqual(fallbackBannerState("fallback", naive49hAgo, NOW), {
    show: true,
    level: "warning",
    age: "known",
  });
  // And a fresh naive stamp stays info.
  const naive1hAgo = new Date(NOW - 60 * 60 * 1000)
    .toISOString()
    .replace(/\.\d{3}Z$/, "");
  assert.equal(fallbackBannerState("fallback", naive1hAgo, NOW).level, "info");
});

// --- CSS cascade guard (review blocker): .fallback-banner sets display:flex
// (author origin), which beats the UA stylesheet's [hidden]{display:none}
// regardless of specificity — without an explicit author-origin [hidden]
// override, the default-hidden banner would render as an empty flex box on
// EVERY load, including live mode. Guard that the override stays in style.css.

test("style.css restates [hidden]{display:none} for the flex banner", async () => {
  const { readFile } = await import("node:fs/promises");
  const css = await readFile(new URL("../style.css", import.meta.url), "utf8");
  const rule = css.match(/\.fallback-banner\[hidden\]\s*\{[^}]*\}/);
  assert.ok(rule, "expected a .fallback-banner[hidden] rule in style.css");
  assert.match(rule[0], /display:\s*none/);
});

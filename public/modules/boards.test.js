// boards.js — the Boards section's catalogue table. Baked-first: the freshness
// column is computed client-side from ttl_days/last_fetched so it works even
// without the live /api/board-statuses endpoint (this module's own baked-first
// framing). We install a minimal DOM shim (matching archive.test.js) and drive
// renderBoards() directly against a synthetic boards_catalog.
//
// location.protocol is "file:" below, so state.js's API_BASE resolves to ""
// (falsy) — loadLiveBoards() returns before fetching, so every test here
// exercises the baked-only render path, which is exactly what needs covering.

import { test } from "node:test";
import assert from "node:assert/strict";

const grid = { innerHTML: "" };
const byId = { boardsGrid: grid };

const catalog = [];
// state.js destructures `groups` from window.VACANCY_DATA once at import and
// boards.js reads that same reference, so mutate this array IN PLACE (never
// reassign) to feed synthetic roles into the browser-derived yield funnel.
const catalogGroups = [];

globalThis.document = {
  getElementById: (id) => byId[id] || null,
};
globalThis.window = {
  VACANCY_DATA: {
    config: { i18n: {}, i18n_all: null, language: "en" },
    stats: {},
    vacancy_ids: [],
    groups: catalogGroups,
    companies: [],
    triage_reviews: [],
    archived_groups: [],
    boards_catalog: catalog,
  },
};
globalThis.location = { protocol: "file:", origin: "" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const { renderBoards, toggleBoard } = await import("./boards.js");

function setGroups(next) {
  catalogGroups.length = 0;
  catalogGroups.push(...next);
}

function boardRow(overrides = {}) {
  return Object.assign(
    {
      id: "board-one",
      name: "Board One",
      audience: "Some audience",
      strategy: "api",
      tier: "B",
      ttl_days: 3,
      url: "https://example.org/board",
      enabled: true,
      last_fetched: "",
    },
    overrides,
  );
}

function setCatalog(next) {
  catalog.length = 0;
  catalog.push(...next);
}

test("renders every board with a name, tier badge and TTL", () => {
  setCatalog([
    boardRow({ id: "a", name: "Alpha", enabled: true, tier: "A" }),
    boardRow({ id: "b", name: "Beta", enabled: false, tier: "C" }),
  ]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(html.includes("brd-row"), "row hook present");
  assert.ok(
    html.includes("Alpha") && html.includes("Beta"),
    "both boards render",
  );
  assert.ok(
    html.includes("vac-tier tier-a"),
    "tier A badge uses the shared tier helper",
  );
  assert.ok(
    html.includes("vac-tier tier-c"),
    "tier C badge uses the shared tier helper",
  );
  assert.ok(html.includes(">3d<"), "TTL renders as a day count");
});

test("the enabled dot distinguishes on/off boards, and off dims the row", () => {
  setCatalog([
    boardRow({ id: "on", name: "OnBoard", enabled: true }),
    boardRow({ id: "off", name: "OffBoard", enabled: false }),
  ]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(html.includes("brd-dot-on"), "enabled board gets the on dot");
  assert.ok(html.includes("brd-dot-off"), "disabled board gets the off dot");
  assert.ok(html.includes("brd-row--off"), "disabled board's row is dimmed");
  // Enabled sorts first regardless of catalogue order.
  assert.ok(html.indexOf("OnBoard") < html.indexOf("OffBoard"));
});

test("freshness is computed baked-first from ttl_days + last_fetched (no live API needed)", () => {
  const now = Date.now();
  const oneDayAgo = new Date(now - 1 * 86400000).toISOString();
  const tenDaysAgo = new Date(now - 10 * 86400000).toISOString();
  setCatalog([
    boardRow({
      id: "never",
      name: "NeverFetched",
      last_fetched: "",
      ttl_days: 3,
    }),
    boardRow({
      id: "fresh",
      name: "FreshBoard",
      last_fetched: oneDayAgo,
      ttl_days: 3,
    }),
    boardRow({
      id: "stale",
      name: "StaleBoard",
      last_fetched: tenDaysAgo,
      ttl_days: 3,
    }),
  ]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(
    html.includes("freshness-never"),
    "never-fetched board gets the never dot",
  );
  assert.ok(
    html.includes("freshness-green"),
    "within-TTL board gets the green dot",
  );
  assert.ok(
    html.includes("freshness-amber"),
    "past-TTL board gets the amber dot",
  );
  // No live API reachable in this test environment, so the vacancy-count
  // columns are absent rather than showing zeroes.
  assert.ok(
    !html.includes("brd-th num"),
    "live-only count columns are omitted",
  );
});

test("empty catalogue shows the empty state, not a blank table", () => {
  setCatalog([]);
  renderBoards();
  assert.ok(grid.innerHTML.includes("brd-empty"));
  assert.ok(!grid.innerHTML.includes("<table"));
});

test("board fields are escaped in both text and attribute positions", () => {
  setCatalog([
    boardRow({
      id: 'evil"id',
      name: "<script>alert(1)</script>",
      audience: 'Aud <b>ience</b> "quoted"',
      url: "https://evil.example/><script>alert(2)</script>",
      strategy: "<i>api</i>",
    }),
  ]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(!html.includes("<script>"), "board name script tag is neutralized");
  assert.ok(!html.includes("<b>ience</b>"), "audience markup is neutralized");
  assert.ok(!html.includes("<i>api</i>"), "strategy markup is neutralized");
  assert.ok(
    html.includes(
      'href="https://evil.example/&gt;&lt;script&gt;alert(2)&lt;/script&gt;"',
    ),
    "the href attribute value itself is escaped, not raw",
  );
  assert.ok(html.includes("&quot;id"), "the id chip escapes embedded quotes");
});

// Simple mode (this file runs under file:// → API_BASE ""): the enabled dot
// stays a read-only span, no interactive toggle, and the CLI hint is shown —
// mirrors how the other write actions degrade offline.
test("simple mode renders a read-only dot, no toggle button, and the CLI hint", () => {
  setCatalog([boardRow({ id: "a", name: "Alpha", enabled: true })]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(html.includes("brd-dot-on"), "the read-only dot still renders");
  assert.ok(!html.includes("brd-toggle"), "no toggle button in simple mode");
  assert.ok(
    !html.includes("aria-pressed"),
    "no toggle semantics in simple mode",
  );
  assert.ok(html.includes("boards-cli-hint"), "the CLI hint is shown offline");
  assert.ok(
    !html.includes("boards-toggle-note"),
    "the interactive note is not shown offline",
  );
});

test("simple mode: toggleBoard is a no-op (no API to write to)", async () => {
  setCatalog([boardRow({ id: "a", name: "Alpha", enabled: true })]);
  const ok = await toggleBoard("a", false);
  assert.equal(ok, false, "returns false — nothing was written");
  assert.equal(catalog[0].enabled, true, "the catalogue entry is untouched");
});

test("an unsafe URL scheme never reaches an href", () => {
  setCatalog([
    boardRow({ id: "unsafe", name: "UnsafeBoard", url: "javascript:alert(1)" }),
  ]);
  setGroups([]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(
    !html.includes("javascript:"),
    "unsafe scheme never reaches the DOM",
  );
  // The board NAME must not be wrapped in a link when its URL is unsafe. (The
  // always-present, hardcoded "suggest a board" link is a separate safe anchor,
  // so we scope the check to the board name rather than the whole table.)
  assert.ok(html.includes("UnsafeBoard"), "the board name still renders");
  assert.ok(
    !html.includes(">UnsafeBoard</a>"),
    "no link renders around a board whose only URL is unsafe",
  );
});

// ---------------------------------------------------------------------------
// Per-board yield (scored → fit → liked) — DERIVED in the browser, so it renders
// in this baked-only (file://) test with no live API.
// ---------------------------------------------------------------------------

test("the yield column renders in simple mode (derived, not live-gated)", () => {
  setCatalog([boardRow({ id: "y", name: "YieldBoard" })]);
  setGroups([]);
  renderBoards();
  const html = grid.innerHTML;
  assert.ok(
    html.includes("brd-th-yield"),
    "the yield header column is present",
  );
  // It is NOT a live-only column, so it survives even with no /api reachable.
  assert.ok(!html.includes("brd-th num"), "live count columns still omitted");
});

test("a board with matching scored roles shows its funnel numbers", () => {
  setCatalog([boardRow({ id: "alpha", name: "Alpha" })]);
  setGroups([
    { id: "a1", source_board: "Alpha", llm_score: 80, member_ids: [] }, // fit
    { id: "a2", source_board: "Alpha", llm_score: 40, member_ids: [] }, // scored, not fit
    { id: "b1", source_board: "Other", llm_score: 70, member_ids: [] }, // different board
  ]);
  renderBoards();
  const html = grid.innerHTML;
  assert.ok(html.includes("brd-yield-funnel"), "the funnel renders");
  assert.ok(
    !html.includes("brd-yield-empty"),
    "no empty state when data exists",
  );
  // 2 scored / 1 fit; the numbers ride in <b> tags.
  assert.ok(html.includes("<b>2</b>"), "scored count is shown");
  assert.ok(html.includes("<b>1</b>"), "fit count is shown");
});

test("a board with no matching roles shows an honest 'no data yet', not 0/0/0", () => {
  setCatalog([boardRow({ id: "cold", name: "ColdBoard" })]);
  setGroups([
    { id: "x", source_board: "SomeOtherBoard", llm_score: 90, member_ids: [] },
  ]);
  renderBoards();
  const html = grid.innerHTML;
  assert.ok(
    html.includes("brd-yield-empty"),
    "cold board gets the no-data state",
  );
  assert.ok(!html.includes("brd-yield-funnel"), "and not a 0/0/0 funnel");
});

test("the Boards section links to the suggest-a-board issue form", () => {
  setCatalog([boardRow({})]);
  setGroups([]);
  renderBoards();
  assert.ok(
    grid.innerHTML.includes("issues/new?template=suggest-a-board.yml"),
    "a link to the GitHub issue form is present",
  );
});

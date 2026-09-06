// derive.js — the shared client-side derivation layer. Pure functions with all
// app dependencies injected, so they unit-test directly (DHA-376).

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  VISIBLE_MIN_SCORE,
  clearsScoreFloor,
  hasVerdict,
  isVisible,
  visibleGroups,
  effectiveBasket,
  basketCounts,
  groupsInBasket,
  geoBuckets,
  selectTodayRoles,
  APPLYABLE_MIN_SCORE,
  HOT_MIN_SCORE,
  isApplyable,
  companyRollup,
  boardYield,
} from "./derive.js";

// Mirror of STATUS_BASKET in state.js (kept inline so the test imports nothing
// that touches window/DOM).
const STATUS_BASKET = {
  liked: "liked",
  to_apply: "liked",
  to_research: "liked",
  to_network: "liked",
  applied: "liked",
  expiring: "liked",
  unseen: "unseen",
  passed: "passed",
  skipped: "passed",
};

const TODAY = "2026-07-03";

// Build the shared opts for the visibility/basket helpers. `statuses` is the
// live overlay (id → status); anything absent is "unseen", exactly like the
// running app's dbData default.
function visOpts(
  statuses,
  { minScore = VISIBLE_MIN_SCORE, today = TODAY } = {},
) {
  const getStatus = (g) => statuses[g.id] || "unseen";
  const isExpired = (g) => {
    if (!g.deadline) return false;
    const dl = new Date(g.deadline);
    if (isNaN(dl.getTime())) return false;
    return dl < new Date(today);
  };
  return {
    isApproved: (g) => g.approved !== false,
    getStatus,
    isExpired,
    basketMap: STATUS_BASKET,
    minScore,
  };
}

// A dataset that exercises every visibility path: below-floor, not-approved,
// multi-location, expired-liked, country-only and remote-only roles.
function sampleGroups() {
  return [
    {
      id: "g1",
      approved: true,
      llm_score: 80,
      locations: [{ city: "Berlin", country: "Germany" }],
    },
    {
      id: "g2",
      approved: true,
      llm_score: 30,
      locations: [{ city: "Berlin", country: "Germany" }],
    },
    {
      id: "g3",
      approved: true,
      llm_score: 90,
      locations: [
        { city: "Berlin", country: "Germany" },
        { city: "Paris", country: "France" },
      ],
    },
    {
      id: "g4",
      approved: true,
      llm_score: 70,
      deadline: "2026-06-01",
      locations: [{ city: "Paris", country: "France" }],
    },
    {
      id: "g5",
      approved: false,
      llm_score: 95,
      locations: [{ city: "Berlin", country: "Germany" }],
    },
    {
      id: "g6",
      approved: true,
      llm_score: 60,
      locations: [{ country: "Spain" }],
    },
    { id: "g7", approved: true, llm_score: 75, locations: [] },
  ];
}

// ---------------------------------------------------------------------------
// Shared visibility filter
// ---------------------------------------------------------------------------

test("the canonical score floor is 40 (mirrors CATALOG_MIN_SCORE)", () => {
  assert.equal(VISIBLE_MIN_SCORE, 40);
});

test("score floor: scored-and-above passes, below/unscored fails, null lifts it", () => {
  assert.equal(clearsScoreFloor({ llm_score: 40 }, 40), true);
  assert.equal(clearsScoreFloor({ llm_score: 39 }, 40), false);
  assert.equal(clearsScoreFloor({ llm_score: null }, 40), false);
  assert.equal(clearsScoreFloor({ llm_score: null }, null), true);
  assert.equal(clearsScoreFloor({ llm_score: 0 }, null), true);
});

test("visible = approved (or strong match) AND clears the floor; expiry is not a visibility gate", () => {
  const opts = visOpts({});
  assert.equal(
    isVisible({ id: "a", approved: true, llm_score: 80 }, opts),
    true,
  );
  // Not approved but a strong match (≥ ANY_COMPANY_MIN_SCORE) ORs past the gate.
  assert.equal(
    isVisible({ id: "b", approved: false, llm_score: 80 }, opts),
    true,
  );
  // Not approved and below the any-company floor → still hidden.
  assert.equal(
    isVisible({ id: "b2", approved: false, llm_score: 20 }, opts),
    false,
  );
  assert.equal(
    isVisible({ id: "c", approved: true, llm_score: 20 }, opts),
    false,
  );
  // Expired but approved + scored → still visible (it gets re-bucketed later).
  assert.equal(
    isVisible(
      { id: "d", approved: true, llm_score: 80, deadline: "2026-06-01" },
      opts,
    ),
    true,
  );
});

test("visibleGroups drops below-floor roles; a strong unapproved match still shows", () => {
  const visible = visibleGroups(sampleGroups(), visOpts({}));
  assert.deepEqual(
    visible.map((g) => g.id).sort(),
    // g2 below floor stays hidden; g5 is unapproved but scores 95 → ORs past
    // the company gate (mirrors score_floor_any_company).
    ["g1", "g3", "g4", "g5", "g6", "g7"],
  );
});

// ---------------------------------------------------------------------------
// Basket assignment + counts (DHA-374)
// ---------------------------------------------------------------------------

test("effectiveBasket re-buckets an expired liked role to passed", () => {
  const opts = visOpts({ g4: "liked" });
  const liveLiked = { id: "x" };
  const expiredLiked = { id: "g4", deadline: "2026-06-01" };
  assert.equal(
    effectiveBasket({ ...liveLiked }, visOpts({ x: "liked" })),
    "liked",
  );
  assert.equal(effectiveBasket(expiredLiked, opts), "passed");
});

test("basket counts over the visible set match the sample by hand", () => {
  const opts = visOpts({ g3: "liked", g4: "liked", g6: "passed" });
  const counts = basketCounts(sampleGroups(), opts);
  // liked: g3 | unseen: g1,g7,g5(unapproved 95, ORs past the gate) | passed: g4(expired-liked)+g6
  assert.deepEqual(counts, { liked: 1, unseen: 3, passed: 2 });
});

// This is the DHA-374 invariant: a basket badge equals the number of rows its
// list renders under the SAME filter, always.
test("DHA-374: every badge count equals its basket list length", () => {
  const opts = visOpts({ g3: "liked", g4: "liked", g6: "passed" });
  const groups = sampleGroups();
  const counts = basketCounts(groups, opts);
  for (const basket of ["liked", "unseen", "passed"]) {
    assert.equal(
      counts[basket],
      groupsInBasket(groups, basket, opts).length,
      `badge vs list mismatch for ${basket}`,
    );
  }
});

// Red→green witness: the OLD badge (approved-only, no score floor) over-counts
// against the floored list; the shared derivation collapses them to one number.
test("DHA-374 red→green: below-floor unseen roles no longer split badge from list", () => {
  // The reported shape: 61 approved, unseen roles, all below the 40 floor.
  const groups = Array.from({ length: 61 }, (_, i) => ({
    id: "u" + i,
    approved: true,
    llm_score: 30,
    locations: [],
  }));
  const opts = visOpts({});

  // OLD badge logic: approved-only, no floor → 61.
  const oldBadge = groups.filter((g) => g.approved !== false).length;
  // OLD list logic: approved + floor → 0.
  const oldList = groups.filter(
    (g) => g.approved !== false && g.llm_score >= VISIBLE_MIN_SCORE,
  ).length;
  assert.notEqual(oldBadge, oldList); // the bug: 61 vs 0

  // NEW shared derivation: badge and list are one computation → both 0, equal.
  const counts = basketCounts(groups, opts);
  assert.equal(counts.unseen, groupsInBasket(groups, "unseen", opts).length);
  assert.equal(counts.unseen, 0);
});

test("lifting the floor (show-all) counts below-floor roles in badge AND list alike", () => {
  const groups = Array.from({ length: 61 }, (_, i) => ({
    id: "u" + i,
    approved: true,
    llm_score: 30,
    locations: [],
  }));
  const opts = visOpts({}, { minScore: null });
  const counts = basketCounts(groups, opts);
  assert.equal(counts.unseen, 61);
  assert.equal(counts.unseen, groupsInBasket(groups, "unseen", opts).length);
});

test("a like changes the derived basket counts with no reload", () => {
  const groups = sampleGroups();
  const before = basketCounts(groups, visOpts({}));
  const after = basketCounts(groups, visOpts({ g1: "liked" }));
  assert.equal(after.liked, before.liked + 1);
  assert.equal(after.unseen, before.unseen - 1);
});

// ---------------------------------------------------------------------------
// An explicit verdict un-floors a role (Nit A) — same principle as Today: a
// role you acted on must never vanish under the discovery score floor.
// ---------------------------------------------------------------------------

test("hasVerdict: any known non-unseen status is a verdict; unseen is not", () => {
  const opts = visOpts({ a: "liked", b: "passed", c: "to_apply", d: "unseen" });
  assert.equal(hasVerdict({ id: "a" }, opts), true);
  assert.equal(hasVerdict({ id: "b" }, opts), true);
  assert.equal(hasVerdict({ id: "c" }, opts), true);
  assert.equal(hasVerdict({ id: "d" }, opts), false); // explicit unseen
  assert.equal(hasVerdict({ id: "e" }, opts), false); // absent → unseen default
});

test("a liked below-floor role is visible; an unseen below-floor role is not", () => {
  const low = { id: "z", approved: true, llm_score: 12, locations: [] };
  // Unseen + below the 40 floor → hidden from the discovery surfaces.
  assert.equal(isVisible(low, visOpts({})), false);
  // The user likes it → the verdict overrides the floor, it stays visible.
  assert.equal(isVisible(low, visOpts({ z: "liked" })), true);
  // A pass is just as explicit → still visible (surfaces in Passed).
  assert.equal(isVisible(low, visOpts({ z: "passed" })), true);
});

test("Nit A: liking/passing a below-floor role puts it in that basket's count+list", () => {
  const groups = [
    { id: "lo_like", approved: true, llm_score: 10, locations: [] },
    { id: "lo_pass", approved: true, llm_score: 15, locations: [] },
    { id: "lo_unseen", approved: true, llm_score: 20, locations: [] },
  ];
  const opts = visOpts({ lo_like: "liked", lo_pass: "passed" });
  const counts = basketCounts(groups, opts);
  // The acted-on low scorers surface in their baskets; the undecided one is
  // still floored out.
  assert.deepEqual(counts, { liked: 1, unseen: 0, passed: 1 });
  // Badge == list holds for the un-floored roles too.
  assert.deepEqual(
    groupsInBasket(groups, "liked", opts).map((g) => g.id),
    ["lo_like"],
  );
  assert.deepEqual(
    groupsInBasket(groups, "passed", opts).map((g) => g.id),
    ["lo_pass"],
  );
});

test("un-approved company: a strong match shows, a below-floor role stays hidden", () => {
  // ≥ ANY_COMPANY_MIN_SCORE ORs past the approval gate (score_floor_any_company).
  const strong = { id: "np", approved: false, llm_score: 90, locations: [] };
  assert.equal(isVisible(strong, visOpts({ np: "liked" })), true);
  // Below the floor, approval still gates it — a verdict cannot un-hide it.
  const weak = { id: "nw", approved: false, llm_score: 20, locations: [] };
  assert.equal(isVisible(weak, visOpts({ nw: "liked" })), false);
});

// ---------------------------------------------------------------------------
// Geo aggregation
// ---------------------------------------------------------------------------

function byKey(rows) {
  const m = {};
  for (const r of rows) m[r.key] = r;
  return m;
}

test("Geo buckets by city over the visible set, counting a role once per location", () => {
  const rows = byKey(
    geoBuckets(
      sampleGroups(),
      visOpts({ g3: "liked", g4: "liked", g6: "passed" }),
    ),
  );
  // Berlin: g1 + g3 + g5 (unapproved 95, ORs past the gate) → count 3, liked 1
  // (g3), mean of 80,90,95 = 88.3.
  assert.equal(rows["Germany::Berlin"].count, 3);
  assert.equal(rows["Germany::Berlin"].liked, 1);
  assert.equal(rows["Germany::Berlin"].meanScore, 88.3);
  // Paris: g3 + g4 → count 2; g4 is expired-liked so NOT liked → liked 1.
  assert.equal(rows["France::Paris"].count, 2);
  assert.equal(rows["France::Paris"].liked, 1);
  assert.equal(rows["France::Paris"].meanScore, 80);
  // Spain: country-only (raw city ""), one role.
  assert.equal(rows["Spain::"].count, 1);
  assert.equal(rows["Spain::"].city, "");
  // Remote/unknown bucket for the location-less role.
  assert.equal(rows["__remote_unknown"].count, 1);
  assert.equal(rows["__remote_unknown"].isRemote, true);
  // Below-floor g2 never reaches any bucket; unapproved g5 does (score 95).
  const berlinTotal = rows["Germany::Berlin"].count;
  assert.equal(berlinTotal, 3);
});

test("Geo 'liked' column reacts to a like with no reload", () => {
  const before = byKey(geoBuckets(sampleGroups(), visOpts({})));
  const after = byKey(geoBuckets(sampleGroups(), visOpts({ g1: "liked" })));
  assert.equal(before["Germany::Berlin"].liked, 0);
  assert.equal(after["Germany::Berlin"].liked, 1);
});

test("Geo drops a liked role from 'liked' once its deadline passes", () => {
  // g3 liked, deadline in the near future vs already past.
  const groups = sampleGroups();
  groups.find((g) => g.id === "g3").deadline = "2026-07-10";
  const opts = (today) => visOpts({ g3: "liked" }, { today });
  const beforeExpiry = byKey(geoBuckets(groups, opts("2026-07-03")));
  const afterExpiry = byKey(geoBuckets(groups, opts("2026-07-11")));
  assert.equal(beforeExpiry["Germany::Berlin"].liked, 1);
  assert.equal(afterExpiry["Germany::Berlin"].liked, 0); // expired → not liked
});

// ---------------------------------------------------------------------------
// Today cockpit
// ---------------------------------------------------------------------------

// The Today rework (DHA-410): six ordered, hide-when-empty populations. opts
// inject isApproved / getStatus / isLiveRole / daysUntil / soonDays — no
// basketMap, no prevVisit (the old "new since last visit" list is gone).
function todayOpts(statuses, { today = TODAY } = {}) {
  const getStatus = (g) => statuses[g.id] || "unseen";
  const daysUntil = (dateStr) => {
    if (!dateStr) return null;
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return null;
    return Math.floor((d.getTime() - new Date(today).getTime()) / 86400000);
  };
  const isExpired = (g) => {
    if (!g.deadline) return false;
    const dl = new Date(g.deadline);
    return !isNaN(dl.getTime()) && dl < new Date(today);
  };
  const isLiveRole = (g) => {
    const s = getStatus(g);
    if (s === "archived" || s === "passed" || s === "skipped") return false;
    if (s === "expiring") return true;
    return !isExpired(g);
  };
  return {
    isApproved: (g) => g.approved !== false,
    getStatus,
    isLiveRole,
    daysUntil,
    soonDays: 7,
  };
}

// A wide sample touching every block. TODAY = 2026-07-03.
function todaySample() {
  return [
    { id: "cm", approved: true, llm_score: 65, deadline: "2026-07-20" }, // to_apply
    { id: "ov", approved: true, llm_score: 70, deadline: "2026-06-20" }, // to_apply, past deadline → overdue
    { id: "ap", approved: true, llm_score: 80 }, // applied → awaiting
    { id: "lk", approved: true, llm_score: 55, deadline: "2026-07-10" }, // liked, live
    { id: "lx", approved: true, llm_score: 55, deadline: "2026-06-20" }, // liked, deadline passed → drops
    { id: "cs", approved: true, llm_score: 75, deadline: "2026-07-08" }, // unseen 75, 5d → closing soon
    { id: "hi", approved: true, llm_score: 30, deadline: "2026-07-07" }, // unseen weak, 4d → hidden count
    { id: "un", approved: true, llm_score: null, deadline: "2026-07-05" }, // unseen unscored, 2d → hidden count
    { id: "rt", approved: true, llm_score: 80, first_seen: "2026-06-23" }, // unseen 80, aged 10d, no deadline → rot
    {
      id: "both",
      approved: true,
      llm_score: 90,
      deadline: "2026-07-08",
      first_seen: "2026-06-13",
    }, // unseen 90, near deadline + aged 20d → closing soon only (AE1)
    { id: "rs", approved: true, llm_score: 62 }, // to_research → working
    { id: "nw", approved: true, llm_score: 62 }, // to_network → working
    { id: "no", approved: false, llm_score: 95, deadline: "2026-07-08" }, // unapproved → nowhere
  ];
}

const todayStatuses = {
  cm: "to_apply",
  ov: "to_apply",
  ap: "applied",
  lk: "liked",
  lx: "liked",
  rs: "to_research",
  nw: "to_network",
  // cs/hi/un/rt/both/no default to unseen
};

test("Today: committed = to_apply, past-deadline kept and flagged overdue", () => {
  const { committed } = selectTodayRoles(
    todaySample(),
    todayOpts(todayStatuses),
  );
  assert.deepEqual(
    committed.map((r) => r.g.id),
    ["ov", "cm"], // overdue (-13) sorts before the future deadline
  );
  assert.equal(committed[0].overdue, true);
  assert.equal(committed[1].overdue, false);
});

test("Today: awaiting = applied roles (read directly from status)", () => {
  const { awaiting } = selectTodayRoles(
    todaySample(),
    todayOpts(todayStatuses),
  );
  assert.deepEqual(
    awaiting.map((g) => g.id),
    ["ap"],
  );
});

test("Today: liked keeps live roles; a liked role whose deadline passed drops (R5, AE3)", () => {
  const { liked } = selectTodayRoles(todaySample(), todayOpts(todayStatuses));
  assert.deepEqual(
    liked.map((g) => g.id),
    ["lk"], // lx dropped — deadline lapsed
  );
});

test("Today: closing soon = unseen, score ≥60, deadline ≤7d — and wins the overlap with don't-rot (AE1)", () => {
  const { closingSoon, dontRot } = selectTodayRoles(
    todaySample(),
    todayOpts(todayStatuses),
  );
  assert.deepEqual(
    closingSoon.map((r) => r.g.id),
    ["cs", "both"], // both are 5d out; equal deadline → stable order
  );
  assert.ok(closingSoon.every((r) => r.expiring === false));
  // `both` qualifies for both closing-soon and don't-rot, but shows only in
  // closing soon (dedupe upward); `rt` is the only pure don't-rot role.
  assert.deepEqual(
    dontRot.map((g) => g.id),
    ["rt"],
  );
});

test("Today: closingSoonHidden counts the weak/unscored near-deadline roles the gate hides (R4)", () => {
  const { closingSoon, closingSoonHidden } = selectTodayRoles(
    todaySample(),
    todayOpts(todayStatuses),
  );
  assert.equal(closingSoonHidden, 2); // hi (score 30) + un (unscored)
  // The hidden ones are NOT in the visible closing-soon list.
  assert.ok(!closingSoon.some((r) => r.g.id === "hi" || r.g.id === "un"));
});

test("Today: protected 'expiring' roles lead the Closing-soon block, flagged (never lost)", () => {
  const groups = todaySample();
  groups.push({ id: "px", approved: true, llm_score: 72 }); // status expiring
  const { closingSoon } = selectTodayRoles(
    groups,
    todayOpts({ ...todayStatuses, px: "expiring" }),
  );
  // px leads despite having no deadline; the unseen deadline rows follow.
  assert.deepEqual(
    closingSoon.map((r) => r.g.id),
    ["px", "cs", "both"],
  );
  assert.equal(closingSoon[0].expiring, true);
});

test("Today: an 'expiring' role is never score-gated — a weak protected role still surfaces", () => {
  const groups = [{ id: "weak", approved: true, llm_score: 12 }];
  const { closingSoon } = selectTodayRoles(
    groups,
    todayOpts({ weak: "expiring" }),
  );
  assert.deepEqual(
    closingSoon.map((r) => r.g.id),
    ["weak"],
  );
});

test("Today: working = to_research / to_network, live only", () => {
  const { working } = selectTodayRoles(todaySample(), todayOpts(todayStatuses));
  assert.deepEqual(working.map((g) => g.id).sort(), ["nw", "rs"]);
});

test("Today: an unapproved role never appears in any block", () => {
  const all = selectTodayRoles(todaySample(), todayOpts(todayStatuses));
  const ids = [
    ...all.committed.map((r) => r.g.id),
    ...all.awaiting.map((g) => g.id),
    ...all.liked.map((g) => g.id),
    ...all.closingSoon.map((r) => r.g.id),
    ...all.dontRot.map((g) => g.id),
    ...all.working.map((g) => g.id),
  ];
  assert.ok(!ids.includes("no"));
});

test("Today: every population is an array — empty inputs hide-when-empty (R1)", () => {
  const empty = selectTodayRoles([], todayOpts({}));
  assert.deepEqual(empty.committed, []);
  assert.deepEqual(empty.awaiting, []);
  assert.deepEqual(empty.liked, []);
  assert.deepEqual(empty.closingSoon, []);
  assert.deepEqual(empty.dontRot, []);
  assert.deepEqual(empty.working, []);
  assert.equal(empty.closingSoonHidden, 0);
});

test("Today is not score-floored: a liked low-score role still surfaces", () => {
  const groups = todaySample();
  groups.find((g) => g.id === "lk").llm_score = 12;
  const { liked } = selectTodayRoles(groups, todayOpts({ lk: "liked" }));
  assert.ok(liked.some((g) => g.id === "lk"));
});

test("Today membership reacts to a pass with no reload", () => {
  const base = todaySample();
  const withCommitted = selectTodayRoles(base, todayOpts({ cm: "to_apply" }));
  const afterPass = selectTodayRoles(base, todayOpts({ cm: "passed" }));
  assert.deepEqual(
    withCommitted.committed.map((r) => r.g.id),
    ["cm"],
  );
  assert.deepEqual(
    afterPass.committed.map((r) => r.g.id),
    [],
  );
});

test("Today: closing-soon reacts to a deadline lapsing with no run", () => {
  const groups = todaySample();
  // `cs` is unseen with a 07-08 deadline: closing-soon on the 3rd, gone by the 9th.
  const before = selectTodayRoles(
    groups,
    todayOpts({}, { today: "2026-07-03" }),
  );
  const after = selectTodayRoles(
    groups,
    todayOpts({}, { today: "2026-07-09" }),
  );
  assert.ok(before.closingSoon.some((r) => r.g.id === "cs"));
  assert.ok(!after.closingSoon.some((r) => r.g.id === "cs")); // deadline passed
});

// A stale-source-aware isLiveRole, mirroring today.js `_isLiveRole`: a role whose
// source stopped confirming it STALE_SOURCE_DAYS+ days ago is no longer live.
// Exercises the branch selectTodayRoles delegates to via opts.isLiveRole for the
// liked/working blocks (the default todayOpts mock only checks the deadline).
const STALE_SOURCE_DAYS = 14; // mirrors helpers.STALE_SOURCE_DAYS
function staleAwareTodayOpts(statuses, cfg = {}) {
  const base = todayOpts(statuses, cfg);
  const today = cfg.today || TODAY;
  const sourceAgeDays = (lastSeen) => {
    if (!lastSeen) return null;
    const d = new Date(lastSeen);
    if (isNaN(d.getTime())) return null;
    return Math.floor((new Date(today).getTime() - d.getTime()) / 86400000);
  };
  const isLiveRole = (g) => {
    const s = base.getStatus(g);
    if (s === "archived" || s === "passed" || s === "skipped") return false;
    if (s === "expiring") return true;
    if (base.daysUntil(g.deadline) != null && base.daysUntil(g.deadline) < 0) {
      return false;
    }
    const age = sourceAgeDays(g.last_seen);
    if (age != null && age >= STALE_SOURCE_DAYS) return false; // stale source
    return true;
  };
  return { ...base, isLiveRole };
}

test("Today: a stale-source liked role drops, a fresh one stays (STALE_SOURCE_DAYS)", () => {
  const groups = [
    { id: "fresh", approved: true, llm_score: 80, last_seen: "2026-07-03" },
    { id: "stale", approved: true, llm_score: 80, last_seen: "2026-06-13" },
  ];
  const opts = staleAwareTodayOpts({ fresh: "liked", stale: "liked" });
  const { liked } = selectTodayRoles(groups, opts);
  assert.deepEqual(
    liked.map((g) => g.id),
    ["fresh"], // stale dropped by the staleness branch
  );
});

test("Today keeps a protected 'expiring' role even when its source is stale", () => {
  // A protected role gone stale 30 days ago must still surface — it exists to
  // demand a decision, so the staleness branch is bypassed for status
  // 'expiring' and it leads the Closing-soon block.
  const groups = [
    { id: "prot", approved: true, llm_score: 50, last_seen: "2026-06-03" },
  ];
  const opts = staleAwareTodayOpts({ prot: "expiring" });
  const { closingSoon } = selectTodayRoles(groups, opts);
  assert.ok(closingSoon.some((r) => r.g.id === "prot" && r.expiring === true));
});

test("Today: a stale-source committed role stays, flagged overdue (never silently dropped)", () => {
  const groups = [
    { id: "fresh", approved: true, llm_score: 80, last_seen: "2026-07-03" },
    { id: "stale", approved: true, llm_score: 80, last_seen: "2026-06-13" },
  ];
  const opts = staleAwareTodayOpts({ fresh: "to_apply", stale: "to_apply" });
  const { committed } = selectTodayRoles(groups, opts);
  const byId = Object.fromEntries(committed.map((r) => [r.g.id, r]));
  assert.equal(committed.length, 2); // both kept — the user committed to them
  assert.equal(byId.fresh.overdue, false);
  assert.equal(byId.stale.overdue, true); // stale source → flagged, not hidden
});

// ---------------------------------------------------------------------------
// Company rollups (DHA-407/408)
// ---------------------------------------------------------------------------

// getStatus + isExpired only — the two dependencies companyRollup/isApplyable
// inject. Mirrors companies.js _decorateRollups' opts (getGroupStatus +
// isVacancyExpired == deadline in the past).
function rollupOpts(statuses, { today = TODAY } = {}) {
  return {
    getStatus: (g) => statuses[g.id] || "unseen",
    isExpired: (g) => {
      if (!g.deadline) return false;
      const dl = new Date(g.deadline);
      if (isNaN(dl.getTime())) return false;
      return dl < new Date(today);
    },
  };
}

// A company with the full range: two applyable roles, a sub-floor scored role,
// an unscored role, and a high-score role whose deadline has already passed.
function companyRoles() {
  return [
    { id: "v1", llm_score: 80, deadline: "2026-07-10" }, // applyable, future deadline
    { id: "v2", llm_score: 62 }, // applyable, no deadline
    { id: "v3", llm_score: 30 }, // scored but below the applyable floor
    { id: "v4", llm_score: null }, // unscored — ignored by avg + not applyable
    { id: "v5", llm_score: 90, deadline: "2026-06-01" }, // strongest, but expired
  ];
}

test("the applyable floor is 60 and the hot floor is 55 (mirror config/data_prep)", () => {
  assert.equal(APPLYABLE_MIN_SCORE, 60);
  assert.equal(HOT_MIN_SCORE, 55);
});

test("isApplyable: score floor, decided/removed statuses, and expiry all gate it", () => {
  const opts = rollupOpts({ done: "passed", gone: "applied", old: "unseen" });
  assert.equal(isApplyable({ id: "a", llm_score: 60 }, opts), true);
  assert.equal(isApplyable({ id: "a", llm_score: 59 }, opts), false); // below floor
  assert.equal(isApplyable({ id: "a", llm_score: null }, opts), false); // unscored
  assert.equal(isApplyable({ id: "done", llm_score: 90 }, opts), false); // passed
  assert.equal(isApplyable({ id: "gone", llm_score: 90 }, opts), false); // applied
  assert.equal(
    isApplyable({ id: "old", llm_score: 90, deadline: "2026-06-01" }, opts),
    false, // deadline in the past
  );
  assert.equal(
    isApplyable({ id: "old", llm_score: 90, deadline: "2026-08-01" }, opts),
    true, // future deadline, undecided, above floor
  );
});

test("companyRollup derives count, applyable, avg and hot from the raw roles", () => {
  const r = companyRollup(companyRoles(), rollupOpts({}));
  assert.equal(r.vacancy_count, 5); // every role, scored or not
  assert.equal(r.applyable_count, 2); // v1 + v2 (v3 below floor, v4 unscored, v5 expired)
  assert.equal(r.avg_llm_score, 65.5); // mean of 80,62,30,90 (v4 excluded)
  assert.deepEqual(r.hot, { score: 90, deadline: "2026-06-01" }); // strongest scored role
});

test("companyRollup: applyable reacts to a pass with no reload", () => {
  const before = companyRollup(companyRoles(), rollupOpts({}));
  const after = companyRollup(companyRoles(), rollupOpts({ v2: "passed" }));
  assert.equal(before.applyable_count, 2);
  assert.equal(after.applyable_count, 1); // passing v2 drops it live
});

test("companyRollup: applyable reacts to a deadline lapsing with no run", () => {
  const before = companyRollup(
    companyRoles(),
    rollupOpts({}, { today: "2026-07-03" }),
  );
  const after = companyRollup(
    companyRoles(),
    rollupOpts({}, { today: "2026-07-11" }),
  );
  assert.equal(before.applyable_count, 2);
  assert.equal(after.applyable_count, 1); // v1's deadline (07-10) now passed
});

test("companyRollup: no scored roles → avg null and no hot signal", () => {
  const r = companyRollup(
    [
      { id: "x", llm_score: null },
      { id: "y", llm_score: -1 },
    ],
    rollupOpts({}),
  );
  assert.equal(r.vacancy_count, 2);
  assert.equal(r.applyable_count, 0);
  assert.equal(r.avg_llm_score, null);
  assert.equal(r.hot, null);
});

test("companyRollup: a strongest score below the hot floor yields no hot signal", () => {
  const r = companyRollup([{ id: "x", llm_score: 50 }], rollupOpts({}));
  assert.equal(r.hot, null); // 50 < HOT_MIN_SCORE (55)
});

test("companyRollup: empty company → all-zero rollup, never a crash", () => {
  const r = companyRollup([], rollupOpts({}));
  assert.deepEqual(r, {
    vacancy_count: 0,
    applyable_count: 0,
    avg_llm_score: null,
    hot: null,
  });
});

// ---------------------------------------------------------------------------
// boardYield — per-board funnel (scored → fit → liked) over the user's history.
// ---------------------------------------------------------------------------

// boardYield needs getStatus + isExpired + basketMap (like the app wires it).
function yieldOpts(statuses, { today = TODAY } = {}) {
  return { ...rollupOpts(statuses, { today }), basketMap: STATUS_BASKET };
}

test("boardYield: buckets roles by source_board and counts scored/fit/liked", () => {
  const groups = [
    { id: "a1", source_board: "Alpha", llm_score: 80 }, // fit
    { id: "a2", source_board: "Alpha", llm_score: 40 }, // scored, not fit
    { id: "a3", source_board: "Alpha", llm_score: 62 }, // fit + liked
    { id: "b1", source_board: "Beta", llm_score: 70 }, // fit
  ];
  const y = boardYield(groups, yieldOpts({ a3: "liked" }));
  assert.deepEqual(y.Alpha, { scored: 3, fit: 2, liked: 1, hasData: true });
  assert.deepEqual(y.Beta, { scored: 1, fit: 1, liked: 0, hasData: true });
});

test("boardYield: fit uses the ≥60 apply bar (APPLYABLE_MIN_SCORE)", () => {
  const groups = [
    { id: "x1", source_board: "Board", llm_score: 59 }, // just under
    { id: "x2", source_board: "Board", llm_score: 60 }, // exactly the bar
  ];
  const y = boardYield(groups, yieldOpts({}));
  assert.equal(y.Board.scored, 2);
  assert.equal(y.Board.fit, 1); // only the 60 clears the bar
});

test("boardYield: direct-ATS roles (no source_board) belong to no board", () => {
  const groups = [
    { id: "d1", source_board: "", llm_score: 90 },
    { id: "d2", llm_score: 90 }, // field absent entirely
    { id: "d3", source_board: "Board", llm_score: 90 },
  ];
  const y = boardYield(groups, yieldOpts({}));
  assert.deepEqual(Object.keys(y), ["Board"]);
  assert.equal(y.Board.scored, 1);
});

test("boardYield: an expired like has lapsed — it no longer counts as liked", () => {
  const groups = [
    { id: "e1", source_board: "Board", llm_score: 80, deadline: "2026-06-01" },
  ];
  // Status is liked, but the deadline is in the past (< TODAY) → effectiveBasket
  // rebuckets it to passed, so liked must be 0 (mirrors the badge/list rule).
  const y = boardYield(groups, yieldOpts({ e1: "liked" }));
  assert.equal(y.Board.scored, 1);
  assert.equal(y.Board.liked, 0);
});

test("boardYield: a board with no shipped roles is simply absent (renders no-data)", () => {
  const y = boardYield(
    [{ id: "z", source_board: "Other", llm_score: 70 }],
    yieldOpts({}),
  );
  assert.equal(y.Empty, undefined); // caller shows "no data yet" for absent boards
  assert.equal(y.Other.hasData, true);
});

test("boardYield: empty group set → empty map, never a crash", () => {
  assert.deepEqual(boardYield([], yieldOpts({})), {});
});

// --- Screen view (bulk screening inbox): lists and groups --------------------

import { screenLists, screenGroups, SCREEN_GROUP_KEYS } from "./derive.js";

const ready = (id, extra) =>
  Object.assign({ id, screening_state: "ready", screening: null }, extra);

test("screenLists: ready roles split by status; an unprepared role lands in no list", () => {
  const roles = [
    ready("a"),
    ready("b"),
    ready("c"),
    { id: "d", screening_state: null },
  ];
  const status = { a: "unseen", b: "liked", c: "passed", d: "unseen" };
  const lists = screenLists(roles, (g) => status[g.id]);
  assert.deepEqual([...lists.toScreen], ["a"]);
  assert.deepEqual([...lists.kept], ["b"]);
  assert.deepEqual([...lists.putAside], ["c"]);
});

test("screenLists: a failed role is in no list", () => {
  const lists = screenLists([{ id: "f", screening_state: "failed" }], () => "unseen");
  assert.equal(lists.toScreen.size + lists.kept.size + lists.putAside.size, 0);
});

test("screenGroups: a role with a Spanish requirement and an onsite constraint is in both groups and once in All", () => {
  const roles = [
    ready("a", {
      screening: {
        posting_facts: {
          work_mode: "onsite",
          seniority: "unknown",
          requirements: [
            { kind: "language", value: "Spanish C1", strength: "required", quote: "Spanish C1 required." },
          ],
        },
        profile_comparison: [],
        unknowns: [],
      },
    }),
    ready("b", {
      screening: {
        posting_facts: { work_mode: "remote", seniority: "senior", requirements: [] },
        unknowns: ["eligible countries not stated"],
      },
    }),
  ];
  const g = screenGroups(roles);
  assert.deepEqual(SCREEN_GROUP_KEYS, ["language", "onsite", "seniority", "unclear", "all"]);
  assert.deepEqual([...g.language], ["a"]);
  assert.deepEqual([...g.onsite], ["a"]);
  assert.deepEqual([...g.seniority], ["b"]);
  assert.deepEqual([...g.unclear], ["b"]);
  assert.deepEqual([...g.all], ["a", "b"]);
});

test("screenGroups: a location or authorisation requirement also counts as an onsite/location constraint", () => {
  const roles = [
    ready("a", {
      screening: {
        posting_facts: {
          requirements: [{ kind: "authorisation", value: "UK right to work", strength: "required", quote: "" }],
        },
      },
    }),
  ];
  assert.deepEqual([...screenGroups(roles).onsite], ["a"]);
});

test("screenGroups: a ready role with no screening facts sits only in All", () => {
  const g = screenGroups([ready("z")]);
  assert.equal(g.language.size + g.onsite.size + g.seniority.size + g.unclear.size, 0);
  assert.deepEqual([...g.all], ["z"]);
});

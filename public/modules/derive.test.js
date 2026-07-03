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

test("visible = approved AND clears the floor; expiry is not a visibility gate", () => {
  const opts = visOpts({});
  assert.equal(
    isVisible({ id: "a", approved: true, llm_score: 80 }, opts),
    true,
  );
  assert.equal(
    isVisible({ id: "b", approved: false, llm_score: 80 }, opts),
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

test("visibleGroups drops below-floor and unapproved roles", () => {
  const visible = visibleGroups(sampleGroups(), visOpts({}));
  assert.deepEqual(
    visible.map((g) => g.id).sort(),
    ["g1", "g3", "g4", "g6", "g7"], // g2 below floor, g5 not approved
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
  // liked: g3 | unseen: g1,g7 | passed: g4(expired-liked)+g6
  assert.deepEqual(counts, { liked: 1, unseen: 2, passed: 2 });
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

test("Nit A: an un-approved company still hides a liked role (approval gates all)", () => {
  const g = { id: "np", approved: false, llm_score: 90, locations: [] };
  assert.equal(isVisible(g, visOpts({ np: "liked" })), false);
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
  // Berlin: g1 + g3 → count 2, liked 1 (g3), mean of 80,90 = 85.
  assert.equal(rows["Germany::Berlin"].count, 2);
  assert.equal(rows["Germany::Berlin"].liked, 1);
  assert.equal(rows["Germany::Berlin"].meanScore, 85);
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
  // Below-floor g2 and unapproved g5 never reach any bucket.
  const berlinTotal = rows["Germany::Berlin"].count;
  assert.equal(berlinTotal, 2);
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

function todayOpts(
  statuses,
  { today = TODAY, prevVisit = "2026-07-01T00:00:00Z" } = {},
) {
  const getStatus = (g) => statuses[g.id] || "unseen";
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
  const daysUntil = (dateStr) => {
    if (!dateStr) return null;
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return null;
    return Math.floor((d.getTime() - new Date(today).getTime()) / 86400000);
  };
  return {
    isApproved: (g) => g.approved !== false,
    getStatus,
    basketMap: STATUS_BASKET,
    isLiveRole,
    daysUntil,
    soonDays: 7,
    newHighFit: 70,
    prevVisit,
  };
}

function todaySample() {
  return [
    { id: "t1", approved: true, llm_score: 50 }, // status expiring → protected
    { id: "t2", approved: true, llm_score: 65 }, // status to_apply → ready
    { id: "t3", approved: true, llm_score: 55, deadline: "2026-07-06" }, // liked, deadline in 3d
    { id: "t4", approved: true, llm_score: 75, first_seen: "2026-07-03" }, // unseen new 70+
    { id: "t5", approved: true, llm_score: 65, first_seen: "2026-07-03" }, // unseen but <70
    { id: "t6", approved: true, llm_score: 90 }, // status passed → nothing
    { id: "t7", approved: false, llm_score: 95 }, // to_apply but not approved
  ];
}

test("Today selects protected + soon-deadline into expiring, sorted by urgency", () => {
  const statuses = {
    t1: "expiring",
    t2: "to_apply",
    t3: "liked",
    t6: "passed",
    t7: "to_apply",
  };
  const { expiring } = selectTodayRoles(todaySample(), todayOpts(statuses));
  assert.deepEqual(
    expiring.map((r) => r.g.id),
    ["t1", "t3"], // protected (-1) before deadline (3)
  );
  assert.equal(expiring[0].kind, "protected");
  assert.equal(expiring[1].kind, "deadline");
  assert.equal(expiring[1].daysLeft, 3);
});

test("Today ready = to_apply; new = unseen roles at/above the 70 threshold since last visit", () => {
  const statuses = {
    t1: "expiring",
    t2: "to_apply",
    t3: "liked",
    t6: "passed",
    t7: "to_apply",
  };
  const { ready, newHighFit } = selectTodayRoles(
    todaySample(),
    todayOpts(statuses),
  );
  assert.deepEqual(
    ready.map((g) => g.id),
    ["t2"],
  ); // t7 excluded (unapproved)
  assert.deepEqual(
    newHighFit.map((g) => g.id),
    ["t4"],
  ); // t5 is 65 (<70)
});

test("Today is not score-floored: a liked low-score role still surfaces", () => {
  // t3 scores 55 (above the 40 floor here, but Today never applies the floor) —
  // prove the mechanism by dropping it well below and confirming it stays.
  const groups = todaySample();
  groups.find((g) => g.id === "t3").llm_score = 12;
  const { expiring } = selectTodayRoles(groups, todayOpts({ t3: "liked" }));
  assert.ok(expiring.some((r) => r.g.id === "t3"));
});

test("Today membership reacts to a pass with no reload", () => {
  const base = todaySample();
  const withReady = selectTodayRoles(base, todayOpts({ t2: "to_apply" }));
  const afterPass = selectTodayRoles(base, todayOpts({ t2: "passed" }));
  assert.deepEqual(
    withReady.ready.map((g) => g.id),
    ["t2"],
  );
  assert.deepEqual(
    afterPass.ready.map((g) => g.id),
    [],
  );
});

test("Today drops a liked role from expiring once its deadline passes", () => {
  const statuses = { t3: "liked" };
  const before = selectTodayRoles(
    todaySample(),
    todayOpts(statuses, { today: "2026-07-03" }),
  );
  const after = selectTodayRoles(
    todaySample(),
    todayOpts(statuses, { today: "2026-07-07" }),
  );
  assert.ok(before.expiring.some((r) => r.g.id === "t3"));
  assert.ok(!after.expiring.some((r) => r.g.id === "t3")); // deadline lapsed → not live
});

// A stale-source-aware isLiveRole, mirroring today.js `_isLiveRole`: a role whose
// source stopped confirming it STALE_SOURCE_DAYS+ days ago is no longer live
// (except a protected `expiring` role, which stays until decided). This exercises
// the STALE_SOURCE_DAYS branch selectTodayRoles delegates to via opts.isLiveRole —
// the default todayOpts mock only ever checked the deadline, never staleness.
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
    if (s === "expiring") return true; // protected — exempt from staleness
    if (base.daysUntil(g.deadline) != null && base.daysUntil(g.deadline) < 0) {
      return false;
    }
    const age = sourceAgeDays(g.last_seen);
    if (age != null && age >= STALE_SOURCE_DAYS) return false; // stale source
    return true;
  };
  return { ...base, isLiveRole };
}

test("Today drops a stale-source role and keeps a fresh one (STALE_SOURCE_DAYS)", () => {
  // Two to_apply roles: one confirmed by its source today, one gone stale
  // 20 days ago (past the 14-day staleness line). Same for a new-high-fit unseen.
  const groups = [
    {
      id: "fresh_apply",
      approved: true,
      llm_score: 80,
      last_seen: "2026-07-03",
    },
    {
      id: "stale_apply",
      approved: true,
      llm_score: 80,
      last_seen: "2026-06-13",
    },
    {
      id: "fresh_new",
      approved: true,
      llm_score: 75,
      first_seen: "2026-07-03",
      last_seen: "2026-07-03",
    },
    {
      id: "stale_new",
      approved: true,
      llm_score: 75,
      first_seen: "2026-07-03",
      last_seen: "2026-06-13",
    },
  ];
  const opts = staleAwareTodayOpts({
    fresh_apply: "to_apply",
    stale_apply: "to_apply",
  });
  const { ready, newHighFit } = selectTodayRoles(groups, opts);
  assert.deepEqual(
    ready.map((g) => g.id),
    ["fresh_apply"], // stale_apply dropped by the staleness branch
  );
  assert.deepEqual(
    newHighFit.map((g) => g.id),
    ["fresh_new"], // stale_new dropped by the staleness branch
  );
});

test("Today keeps a protected 'expiring' role even when its source is stale", () => {
  // A protected role gone stale 30 days ago must still surface — it needs a
  // decision, so the staleness branch is bypassed for status 'expiring'.
  const groups = [
    { id: "prot", approved: true, llm_score: 50, last_seen: "2026-06-03" },
  ];
  const opts = staleAwareTodayOpts({ prot: "expiring" });
  const { expiring } = selectTodayRoles(groups, opts);
  assert.ok(expiring.some((r) => r.g.id === "prot" && r.kind === "protected"));
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

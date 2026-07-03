// =============================================================================
// derive.js — the single client-side derivation layer.
//
// Every dashboard number is a pure function of (raw vacancy groups + live
// statuses + today's date), read through ONE shared visibility filter, so a
// count can never disagree with the list it summarizes. The pipeline ships raw
// rows; the browser derives the Geo table, basket counts/badges, Today lists,
// section counts and average scores here.
//
// These functions take EVERY app dependency (approval check, status lookup,
// expiry check, basket map) as an injected option — they import nothing from
// state/DOM/i18n, so they unit-test directly under `node --test`.
// =============================================================================

// Canonical score floor for the "visible set" — mirrors CATALOG_MIN_SCORE in
// scripts/config.py. Roles scoring below it, and unscored roles, are hidden
// from the discovery surfaces (Catalog browse, Geo) unless the floor is lifted.
export const VISIBLE_MIN_SCORE = 40;

// A role clears the score floor when it is scored and at/above `minScore`.
// `minScore == null` lifts the floor entirely (the Catalog "show all" toggle),
// so every scored role passes.
export function clearsScoreFloor(g, minScore) {
  if (minScore == null) return true;
  return g.llm_score != null && g.llm_score >= minScore;
}

// Whether the user has cast an explicit verdict on a role. Any known non-unseen
// status (liked / to_apply / … / passed / skipped) is a deliberate action; only
// "unseen" (the default) means undecided. Used to un-floor acted-on roles below.
export function hasVerdict(g, opts) {
  const status = opts.getStatus(g);
  return status !== "unseen" && opts.basketMap[status] != null;
}

// The shared visibility filter. A role is visible when its company is approved
// AND (the user has already acted on it OR it clears the score floor). An
// explicit verdict overrides the floor — a liked role always shows in Liked and
// a passed role in Passed regardless of score, the same principle the Today tab
// uses (a role you acted on must never silently vanish under the discovery
// floor). Only undecided ("unseen") roles are subject to the floor, which keeps
// the Catalog/Geo browse surfaces focused on high-fit unreviewed roles. Company
// approval still gates everything, and expiry is deliberately NOT a visibility
// gate — an expired liked role stays "visible", it is only re-bucketed to Passed
// (see effectiveBasket), so it is counted and surfaced rather than vanishing.
export function isVisible(g, opts) {
  if (!opts.isApproved(g)) return false;
  if (hasVerdict(g, opts)) return true;
  return clearsScoreFloor(g, opts.minScore);
}

export function visibleGroups(groups, opts) {
  return groups.filter((g) => isVisible(g, opts));
}

// The basket a group belongs to right now. An expired role that would sit in
// the "liked" basket moves to "passed" — a lapsed like is no longer an active
// like. This is the single rule the badge, the list and the Geo "liked" column
// all read, so they cannot drift apart.
export function effectiveBasket(g, opts) {
  const basket = opts.basketMap[opts.getStatus(g)] || "unseen";
  if (basket === "liked" && opts.isExpired(g)) return "passed";
  return basket;
}

// Basket counts over the visible set — exactly the rows each basket list would
// render before any org/location/search refinement, so a badge equals its list
// by construction (fixes DHA-374: the badge and the list are now the SAME
// computation over the SAME filtered set).
export function basketCounts(groups, opts) {
  const counts = { liked: 0, unseen: 0, passed: 0 };
  for (const g of visibleGroups(groups, opts)) {
    const b = effectiveBasket(g, opts);
    counts[b] = (counts[b] || 0) + 1;
  }
  return counts;
}

// The visible rows in one basket — the base set a basket list renders (the "of
// N" denominator). Its length equals that basket's badge count.
export function groupsInBasket(groups, basket, opts) {
  return visibleGroups(groups, opts).filter(
    (g) => effectiveBasket(g, opts) === basket,
  );
}

// ---------------------------------------------------------------------------
// Geo aggregation — role availability by city/country over the visible set.
// ---------------------------------------------------------------------------

const GEO_REMOTE_KEY = "__remote_unknown";

// Bucket the visible set by place. A vacancy with N locations is counted N
// times (the table shows availability per city). Buckets carry RAW city/country
// strings (empty string = whole-country or remote) — the renderer applies the
// display labels and flags, keeping this function label/i18n-free. Each bucket:
//   { key, city, country, count, liked, scoreSum, scoreN, meanScore, isRemote }
export function geoBuckets(groups, opts) {
  const buckets = new Map();

  for (const g of visibleGroups(groups, opts)) {
    const liked = effectiveBasket(g, opts) === "liked";
    const score =
      typeof g.llm_score === "number" && g.llm_score >= 0 ? g.llm_score : null;

    const locs = Array.isArray(g.locations) ? g.locations : [];
    const places = [];
    for (const loc of locs) {
      const city = (loc.city || "").trim();
      const country = (loc.country || "").trim();
      if (!city && !country) continue;
      places.push({ city, country });
    }
    if (places.length === 0) places.push({ key: GEO_REMOTE_KEY });

    for (const p of places) {
      const key = p.key || `${p.country}::${p.city}`;
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = {
          key,
          city: p.city || "",
          country: p.country || "",
          count: 0,
          liked: 0,
          scoreSum: 0,
          scoreN: 0,
          isRemote: !!p.key,
        };
        buckets.set(key, bucket);
      }
      bucket.count += 1;
      if (liked) bucket.liked += 1;
      if (score !== null) {
        bucket.scoreSum += score;
        bucket.scoreN += 1;
      }
    }
  }

  for (const b of buckets.values()) {
    b.meanScore = b.scoreN > 0 ? +(b.scoreSum / b.scoreN).toFixed(1) : null;
  }
  return Array.from(buckets.values());
}

// ---------------------------------------------------------------------------
// Company rollups — the per-company numbers the pipeline used to bake.
//
// Each is a pure function of a company's raw roles (the groups the browser
// already holds) + live statuses + today, so a like/pass or a passing day
// changes them with no run, and a company's count can never disagree with the
// roles listed under it (DHA-407). The pipeline now ships only the raw role-id
// list per company; every NUMBER derives here.
// ---------------------------------------------------------------------------

// Mirrors APPLYABLE_SCORE in scripts/config.py: the score a role must clear to
// count as "worth applying to right now".
export const APPLYABLE_MIN_SCORE = 60;
// Mirrors HOT_VACANCY_SCORE in scripts/report/data_prep.py: a candidate company
// hiding a role this strong floats up Pending Review with a 🔥 badge.
export const HOT_MIN_SCORE = 55;

// Statuses that disqualify a role from "applyable now" — already decided
// (passed), removed (archived), already applied, skipped, or protected-but-
// disappearing (expiring). Mirrors _NON_APPLYABLE_STATUSES in
// scripts/report/data_prep.py; deadline-in-the-past is handled via
// opts.isExpired so expiry stays the one shared notion across the dashboard.
const NON_APPLYABLE_STATUSES = new Set([
  "archived",
  "passed",
  "expiring",
  "applied",
  "skipped",
]);

// A role is worth applying to right now when it clears APPLYABLE_MIN_SCORE, the
// user hasn't already decided/removed it, and its deadline hasn't passed.
export function isApplyable(g, opts) {
  if (g.llm_score == null || g.llm_score < APPLYABLE_MIN_SCORE) return false;
  if (NON_APPLYABLE_STATUSES.has(opts.getStatus(g))) return false;
  if (opts.isExpired(g)) return false;
  return true;
}

// One company's live rollup over its raw roles. `roles` is the company's group
// objects (already resolved from its role-id list); `opts` injects getStatus +
// isExpired. Returns the numbers the Companies section renders:
//   { vacancy_count, applyable_count, avg_llm_score, hot }
// where `hot` is { score, deadline } for the single strongest role at/above
// HOT_MIN_SCORE (or null), and avg_llm_score is over scored roles only (null
// when none are scored). No score floor is applied — the roll-up counts every
// role the company has, matching the roles the profile lists under it.
export function companyRollup(roles, opts) {
  let scoreSum = 0;
  let scoreN = 0;
  let applyable = 0;
  let hotScore = -1;
  let hotDeadline = "";
  for (const g of roles) {
    const score =
      typeof g.llm_score === "number" && g.llm_score >= 0 ? g.llm_score : null;
    if (score !== null) {
      scoreSum += score;
      scoreN += 1;
      if (score > hotScore) {
        hotScore = score;
        hotDeadline = g.deadline || "";
      }
    }
    if (isApplyable(g, opts)) applyable += 1;
  }
  return {
    vacancy_count: roles.length,
    applyable_count: applyable,
    avg_llm_score: scoreN > 0 ? +(scoreSum / scoreN).toFixed(1) : null,
    hot:
      hotScore >= HOT_MIN_SCORE
        ? { score: hotScore, deadline: hotDeadline }
        : null,
  };
}

// ---------------------------------------------------------------------------
// Per-board yield — "is this board earning its place?" over the user's own
// history. Each board's funnel (scored → fit → liked) is a pure function of the
// raw shipped roles (each carries source_board + llm_score) plus live statuses,
// so it needs no /api and reacts to a like/pass or a passing deadline with no
// run — the same derive-never-bake path the company rollups take (DHA-360).
//
// The dashboard payload ships only SCORED roles, so "scored" here means
// "reached your scored catalogue"; the total-saved and fresh-14d numbers (which
// also count unscored/archived rows) stay on the live /api/board-statuses feed.
// A board with zero shipped roles reports hasData:false so the renderer can show
// an honest "no data yet" instead of 0/0/0 read as a verdict.
// ---------------------------------------------------------------------------

// Fold the shipped roles into a per-board funnel keyed by source_board (== the
// board's display name / board.name). Returns { [boardName]: { scored, fit,
// liked, hasData } }. `roles` with no source_board are skipped (direct ATS
// fetches belong to no board). `fit` counts roles at/above APPLYABLE_MIN_SCORE;
// `liked` counts roles whose effective basket is "liked" (an expired like has
// already lapsed to "passed", see effectiveBasket), so the funnel narrows
// honestly. `opts` injects getStatus + isExpired + basketMap, exactly like the
// other derivations here.
export function boardYield(groups, opts) {
  const out = {};
  for (const g of groups) {
    const board = (g.source_board || "").trim();
    if (!board) continue;
    let row = out[board];
    if (!row) {
      row = { scored: 0, fit: 0, liked: 0, hasData: true };
      out[board] = row;
    }
    row.scored += 1;
    if (typeof g.llm_score === "number" && g.llm_score >= APPLYABLE_MIN_SCORE) {
      row.fit += 1;
    }
    if (effectiveBasket(g, opts) === "liked") row.liked += 1;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Today cockpit — the few roles that need a decision now.
// ---------------------------------------------------------------------------

// Select the three Today lists from the approved set. Unlike the discovery
// surfaces, Today is NOT score-floored: a role the user has explicitly acted on
// (liked / to_apply) must surface regardless of score. Everything else is read
// live so the lists react to likes/passes and today's expiry with no run.
//
// Injected opts:
//   isApproved(g), getStatus(g), basketMap, isLiveRole(g), daysUntil(dateStr),
//   soonDays, newHighFit, prevVisit (ISO string or null).
// Returns { expiring, ready, newHighFit } where:
//   expiring: [{ g, kind: "protected" | "deadline", daysLeft }] sorted by
//             urgency (protected first, then soonest deadline),
//   ready:    [g] sorted by score desc,
//   newHighFit: [g] sorted by score desc.
export function selectTodayRoles(groups, opts) {
  const expiring = [];
  const ready = [];
  const newHighFit = [];

  for (const g of groups) {
    if (!opts.isApproved(g)) continue;
    const status = opts.getStatus(g);
    const basket = opts.basketMap[status] || "unseen";

    if (status === "expiring") {
      expiring.push({ g, kind: "protected", daysLeft: null });
    } else if (basket === "liked" && opts.isLiveRole(g)) {
      const dleft = opts.daysUntil(g.deadline);
      if (dleft != null && dleft >= 0 && dleft <= opts.soonDays) {
        expiring.push({ g, kind: "deadline", daysLeft: dleft });
      }
    }

    if (status === "to_apply" && opts.isLiveRole(g)) {
      ready.push(g);
    }

    if (
      status === "unseen" &&
      g.llm_score != null &&
      g.llm_score >= opts.newHighFit &&
      g.first_seen &&
      (!opts.prevVisit || g.first_seen > opts.prevVisit.slice(0, 10)) &&
      opts.isLiveRole(g)
    ) {
      newHighFit.push(g);
    }
  }

  // Urgency order: protected (daysLeft null → sort -1) first, then soonest
  // deadline; the action lists by best fit (score descending).
  const byScoreDesc = (a, b) => (b.llm_score || 0) - (a.llm_score || 0);
  const urgency = (r) => (r.daysLeft == null ? -1 : r.daysLeft);
  expiring.sort((a, b) => urgency(a) - urgency(b));
  ready.sort(byScoreDesc);
  newHighFit.sort(byScoreDesc);

  return { expiring, ready, newHighFit };
}

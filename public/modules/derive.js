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

// The shared visibility filter. A role is visible when its company is approved
// AND it clears the score floor. Expiry is deliberately NOT a visibility gate —
// an expired role is still "visible", it is only re-bucketed (see
// effectiveBasket), so it can still be counted and surfaced in the Passed
// basket rather than vanishing.
export function isVisible(g, opts) {
  return opts.isApproved(g) && clearsScoreFloor(g, opts.minScore);
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

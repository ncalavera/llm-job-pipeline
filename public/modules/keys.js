// =============================================================================
// keys.js — Browse keyboard-triage cursor (U15, DHA-399). PURE: no DOM, no
// state.js, no i18n — it imports nothing, so it unit-tests directly under
// `node --test`. The DOM shell that wires j/k/l/x/Enter lives in catalog.js.
//
// The cursor is keyed by VACANCY ID, never by list position. Every 60s poll
// rebuilds the `groups` array with fresh objects (state.js applySnapshot), and
// a poll can insert a higher-scored row above the one under the cursor — so a
// position-keyed cursor would silently point at a different vacancy after a
// refresh (AE5). Keeping the id, and only falling back to a position when the
// id is gone, is the whole point of this module.
//
// It also remembers the id's LAST KNOWN INDEX in the visible list. That index
// is the fallback when the id disappears (the user liked/passed it, or a filter
// dropped it): the cursor lands on whatever row now occupies the nearest
// reachable slot — "the next row" in the common case, the new last row when the
// removed row was at the end.
// =============================================================================

// Which triage actions a basket's rows actually expose — mirrors catalog.js's
// per-basket thumb gating EXACTLY (unseen → like+pass, liked → pass only,
// passed → like only), so `l` in the Liked basket and `x` in the Passed basket
// are no-ops just like their absent buttons. Kept in lock-step with
// catalog.js:catalogRowHtml's `actionsHtml` branch — the source of truth for
// what each basket renders.
export function actionsFor(basket) {
  return {
    like: basket === "unseen" || basket === "passed",
    pass: basket === "unseen" || basket === "liked",
  };
}

// A cursor instance. catalog.js holds one module-level singleton; tests create
// a fresh one each case so no state leaks between them.
export function createCursor() {
  let id = null; // selected vacancy id, or null when the cursor is dormant
  let index = -1; // id's last known position in the visible list (-1 = none)

  // Point the cursor at a position in `ids`, clamping into range. Empty list
  // clears the cursor. Returns the resulting id (or null).
  function selectAt(ids, i) {
    if (!ids.length) {
      id = null;
      index = -1;
      return null;
    }
    const clamped = Math.max(0, Math.min(i, ids.length - 1));
    index = clamped;
    id = ids[clamped];
    return id;
  }

  return {
    get id() {
      return id;
    },
    get index() {
      return index;
    },

    // Explicitly select an id (e.g. a click that also sets the keyboard
    // cursor). Records its index when present; a not-yet-visible id is stored
    // with index -1 so a later reconcile can still keep it if it appears.
    set(nextId, visibleIds) {
      const ids = visibleIds || [];
      id = nextId == null ? null : nextId;
      index = id == null ? -1 : ids.indexOf(id);
      return id;
    },

    // Escape / basket reset — go dormant, no highlight until the next j/k.
    clear() {
      id = null;
      index = -1;
    },

    // j (delta +1) / k (delta -1): step through the CURRENT visible list,
    // clamped at both ends. From a dormant cursor the first press lands on the
    // top row. If the id drifted out of the list without a reconcile, step from
    // its remembered index instead (defensive — the render path reconciles
    // first in practice).
    move(delta, visibleIds) {
      const ids = visibleIds || [];
      if (!ids.length) {
        id = null;
        index = -1;
        return null;
      }
      let cur;
      const at = id == null ? -1 : ids.indexOf(id);
      if (at !== -1) cur = at;
      else if (index >= 0) cur = Math.min(index, ids.length - 1);
      else cur = -1; // dormant → cur+delta lands on row 0 for either direction
      return selectAt(ids, cur + delta);
    },

    // After a data hot-swap, a row removal (like/pass re-buckets), or a filter
    // change. A dormant cursor stays dormant (never auto-selects a row the user
    // didn't ask for). If the id is still visible, keep it and refresh its
    // index — this is the AE5 guarantee: a poll that inserts rows above the
    // cursor moves the id's index but the id, and so the next l/x/Enter, is
    // unchanged. If the id is gone, fall to the nearest reachable index.
    reconcile(visibleIds) {
      const ids = visibleIds || [];
      if (id == null) return null;
      if (!ids.length) {
        id = null;
        index = -1;
        return null;
      }
      const at = ids.indexOf(id);
      if (at !== -1) {
        index = at;
        return id;
      }
      return selectAt(ids, index);
    },
  };
}

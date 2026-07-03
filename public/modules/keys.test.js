// keys.js — Browse keyboard-triage cursor (U15, DHA-399).
//
// The module imports nothing, so no browser shell is needed — just create a
// fresh cursor per case. These cases pin the id-keyed hot-swap semantics (AE5):
// a poll that inserts a row above the cursor must NOT move which vacancy the
// cursor points at.

import { test } from "node:test";
import assert from "node:assert/strict";

import { createCursor, actionsFor } from "./keys.js";

// --- actionsFor: per-basket mirroring (matches catalog.js thumb gating) -----

test("actionsFor(unseen) exposes both like and pass", () => {
  assert.deepEqual(actionsFor("unseen"), { like: true, pass: true });
});

test("actionsFor(liked) exposes pass only — l is a no-op in Liked", () => {
  assert.deepEqual(actionsFor("liked"), { like: false, pass: true });
});

test("actionsFor(passed) exposes like only — x is a no-op in Passed", () => {
  assert.deepEqual(actionsFor("passed"), { like: true, pass: false });
});

test("actionsFor(unknown basket) exposes neither", () => {
  assert.deepEqual(actionsFor("whatever"), { like: false, pass: false });
});

// --- move: activation, stepping, clamping -----------------------------------

test("first j from a dormant cursor lands on the top row", () => {
  const c = createCursor();
  assert.equal(c.id, null);
  assert.equal(c.move(1, ["a", "b", "c"]), "a");
  assert.equal(c.index, 0);
});

test("first k from a dormant cursor also lands on the top row", () => {
  const c = createCursor();
  assert.equal(c.move(-1, ["a", "b", "c"]), "a");
});

test("j steps forward one row", () => {
  const c = createCursor();
  c.move(1, ["a", "b", "c"]); // a
  assert.equal(c.move(1, ["a", "b", "c"]), "b");
  assert.equal(c.move(1, ["a", "b", "c"]), "c");
});

test("k steps back one row", () => {
  const c = createCursor();
  c.set("c", ["a", "b", "c"]);
  assert.equal(c.move(-1, ["a", "b", "c"]), "b");
  assert.equal(c.move(-1, ["a", "b", "c"]), "a");
});

test("move clamps at the bottom end", () => {
  const c = createCursor();
  c.set("c", ["a", "b", "c"]);
  assert.equal(c.move(1, ["a", "b", "c"]), "c"); // stays on last
  assert.equal(c.move(1, ["a", "b", "c"]), "c");
});

test("move clamps at the top end", () => {
  const c = createCursor();
  c.set("a", ["a", "b", "c"]);
  assert.equal(c.move(-1, ["a", "b", "c"]), "a"); // stays on first
  assert.equal(c.move(-1, ["a", "b", "c"]), "a");
});

test("move on an empty list clears the cursor", () => {
  const c = createCursor();
  c.set("a", ["a", "b"]);
  assert.equal(c.move(1, []), null);
  assert.equal(c.id, null);
});

test("move on a single-item list stays on that item", () => {
  const c = createCursor();
  assert.equal(c.move(1, ["only"]), "only");
  assert.equal(c.move(1, ["only"]), "only");
  assert.equal(c.move(-1, ["only"]), "only");
});

// --- reconcile: keep id when still visible (AE5) -----------------------------

test("reconcile keeps the id when it is still visible", () => {
  const c = createCursor();
  c.set("b", ["a", "b", "c"]);
  assert.equal(c.reconcile(["a", "b", "c"]), "b");
  assert.equal(c.index, 1);
});

test("AE5: a poll inserting a row ABOVE the cursor keeps the same id", () => {
  const c = createCursor();
  c.set("V2", ["V1", "V2", "V3"]); // cursor on V2 at index 1
  // A higher-scored row arrives above V2.
  assert.equal(c.reconcile(["Vnew", "V1", "V2", "V3"]), "V2");
  assert.equal(c.index, 2); // index shifted, id did not — l/Enter still hit V2
});

test("AE5 other order: a row inserted BELOW the cursor also keeps the id", () => {
  const c = createCursor();
  c.set("V2", ["V1", "V2", "V3"]);
  assert.equal(c.reconcile(["V1", "V2", "Vnew", "V3"]), "V2");
  assert.equal(c.index, 1);
});

// --- reconcile: fall to nearest index when the id is gone --------------------

test("reconcile falls to the NEXT row's slot when the cursor row is removed", () => {
  const c = createCursor();
  c.set("V2", ["V1", "V2", "V3"]); // index 1
  // V2 gets liked → drops out of the unseen basket.
  assert.equal(c.reconcile(["V1", "V3"]), "V3"); // slot 1 now holds V3
});

test("reconcile falls UP when the LAST row is removed (both directions)", () => {
  const c = createCursor();
  c.set("V3", ["V1", "V2", "V3"]); // index 2
  assert.equal(c.reconcile(["V1", "V2"]), "V2"); // clamps to new last
});

test("reconcile on an empty list clears an active cursor", () => {
  const c = createCursor();
  c.set("V2", ["V1", "V2", "V3"]);
  assert.equal(c.reconcile([]), null);
  assert.equal(c.id, null);
  assert.equal(c.index, -1);
});

test("reconcile leaves a dormant cursor dormant — never auto-selects", () => {
  const c = createCursor();
  assert.equal(c.reconcile(["a", "b", "c"]), null);
  assert.equal(c.id, null);
});

test("move after a removal reconcile steps from the reconciled row", () => {
  const c = createCursor();
  c.set("V2", ["V1", "V2", "V3"]);
  c.reconcile(["V1", "V3"]); // lands on V3 (slot 1)
  assert.equal(c.id, "V3");
  assert.equal(c.move(-1, ["V1", "V3"]), "V1");
});

// --- set / clear -------------------------------------------------------------

test("set records the id and its index; clear goes dormant", () => {
  const c = createCursor();
  c.set("b", ["a", "b", "c"]);
  assert.equal(c.id, "b");
  assert.equal(c.index, 1);
  c.clear();
  assert.equal(c.id, null);
  assert.equal(c.index, -1);
});

test("set(null) clears the cursor", () => {
  const c = createCursor();
  c.set("a", ["a"]);
  c.set(null, ["a"]);
  assert.equal(c.id, null);
});

// --- Enter emits the selected id --------------------------------------------

test("Enter would open the current cursor id (id getter reflects the selection)", () => {
  const c = createCursor();
  c.move(1, ["a", "b", "c"]); // a
  c.move(1, ["a", "b", "c"]); // b
  assert.equal(c.id, "b"); // the id Enter hands the router
});

// route.js — URL <-> route-object mapping. Pure functions, no DOM/history, so
// they unit-test directly under `node --test` (DHA-388, KTD2).

import { test } from "node:test";
import assert from "node:assert/strict";

import { parse, build } from "./route.js";

// --- parse -----------------------------------------------------------------

test("parse reads ?vacancy= into a vacancy route", () => {
  assert.deepEqual(parse("?vacancy=g123"), { screen: "vacancy", id: "g123" });
  // leading "?" is optional
  assert.deepEqual(parse("vacancy=g123"), { screen: "vacancy", id: "g123" });
});

test("parse reads ?company= into a company route", () => {
  assert.deepEqual(parse("?company=acme"), { screen: "company", id: "acme" });
  assert.deepEqual(parse("company=acme"), { screen: "company", id: "acme" });
});

test("a bare URL (no recognised param) is a section route", () => {
  assert.deepEqual(parse(""), { screen: "section" });
  assert.deepEqual(parse("?"), { screen: "section" });
  assert.deepEqual(parse("?foo=bar&baz=1"), { screen: "section" });
});

test("empty param values fall through to a section route", () => {
  assert.deepEqual(parse("?vacancy="), { screen: "section" });
  assert.deepEqual(parse("?company="), { screen: "section" });
  assert.deepEqual(parse("?vacancy=&company="), { screen: "section" });
});

test("vacancy takes precedence when both params are present", () => {
  assert.deepEqual(parse("?company=acme&vacancy=g9"), {
    screen: "vacancy",
    id: "g9",
  });
  assert.deepEqual(parse("?vacancy=g9&company=acme"), {
    screen: "vacancy",
    id: "g9",
  });
});

test("parse never throws on garbage input", () => {
  for (const junk of ["%%%", "%", "=&=&=", "?%zz=%zz", "&&&", "?=novalue"]) {
    assert.doesNotThrow(() => parse(junk));
    assert.equal(parse(junk).screen, "section");
  }
});

test("parse tolerates non-string input", () => {
  assert.deepEqual(parse(undefined), { screen: "section" });
  assert.deepEqual(parse(null), { screen: "section" });
  assert.deepEqual(parse(42), { screen: "section" });
  assert.deepEqual(parse({}), { screen: "section" });
});

// --- build -----------------------------------------------------------------

test("build emits the query string for each detail screen", () => {
  assert.equal(build({ screen: "vacancy", id: "g123" }), "?vacancy=g123");
  assert.equal(build({ screen: "company", id: "acme" }), "?company=acme");
});

test("build returns an empty string for section / invalid routes", () => {
  assert.equal(build({ screen: "section" }), "");
  assert.equal(build(null), "");
  assert.equal(build(undefined), "");
  assert.equal(build("nonsense"), "");
  assert.equal(build({ screen: "vacancy" }), ""); // missing id
  assert.equal(build({ screen: "company", id: "" }), ""); // empty id
});

test("build ignores junk fields, reading only screen + id", () => {
  assert.equal(
    build({ screen: "vacancy", id: "g1", mode: "x", junk: true, id2: "y" }),
    "?vacancy=g1",
  );
});

// --- round-trip / fixpoint --------------------------------------------------

test("build(parse(x)) round-trips for the clean forms", () => {
  for (const x of [
    "?vacancy=g123",
    "?company=acme",
    "",
    "?foo=bar", // bare -> section -> ""
  ]) {
    const once = build(parse(x));
    // Applying the pipeline again is a fixpoint (normalised form is stable).
    assert.equal(build(parse(once)), once, `stable for ${JSON.stringify(x)}`);
  }
});

test("normalisation is a fixpoint even for percent/plus-encoded values", () => {
  // A value with a space normalises to the +-encoded form and then stays put.
  const first = build(parse("?company=a%20b"));
  assert.equal(first, "?company=a+b");
  assert.equal(build(parse(first)), first);
});

test("both-params URL normalises to the vacancy route and stays stable", () => {
  const norm = build(parse("?company=acme&vacancy=g9"));
  assert.equal(norm, "?vacancy=g9");
  assert.equal(build(parse(norm)), norm);
});

// --- popstate-after-two-pushes, modelled at the pure level ------------------
//
// A history stack is a sequence of search strings. Pushing company then vacancy
// then walking two `back` steps must land back on the originating bare section
// — the exact scenario the DOM popstate handler drives, verified here on the
// pure parser so the routing intent is pinned without a browser.

test("a two-push / two-back history walk resolves to the right screens", () => {
  const stack = ["", "?company=acme", "?vacancy=g9"]; // section -> company -> vacancy
  const screens = stack.map((s) => parse(s).screen);
  assert.deepEqual(screens, ["section", "company", "vacancy"]);

  // Two `back` steps pop to index 0 — the originating section.
  let idx = stack.length - 1; // on the vacancy detail
  idx -= 1; // back once -> company profile
  assert.equal(parse(stack[idx]).screen, "company");
  idx -= 1; // back again -> section list
  assert.equal(parse(stack[idx]).screen, "section");
});

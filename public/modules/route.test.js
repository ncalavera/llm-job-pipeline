// route.js — URL <-> route mapping. build(parse(x)) is a fixpoint for every
// URL the app emits: the two detail params and the ?mode= landing param the
// digest's "ready to screen" link uses.

import { test } from "node:test";
import assert from "node:assert/strict";

import { parse, build } from "./route.js";

test("parse: bare URL and garbage collapse to a plain section route", () => {
  assert.deepEqual(parse(""), { screen: "section" });
  assert.deepEqual(parse("?x=1"), { screen: "section" });
  assert.deepEqual(parse(null), { screen: "section" });
});

test("parse: detail params win over mode; vacancy wins over company", () => {
  assert.deepEqual(parse("?vacancy=v1&company=c1&mode=screen"), {
    screen: "vacancy",
    id: "v1",
  });
  assert.deepEqual(parse("?company=c1&mode=screen"), {
    screen: "company",
    id: "c1",
  });
});

test("parse: ?mode=screen lands on the section route with that mode", () => {
  assert.deepEqual(parse("?mode=screen"), { screen: "section", mode: "screen" });
});

test("build(parse(x)) is a fixpoint for every emitted URL", () => {
  for (const url of ["", "?vacancy=v1", "?company=c1", "?mode=screen"]) {
    assert.equal(build(parse(url)), url);
  }
});

test("build: an empty mode builds a bare URL", () => {
  assert.equal(build({ screen: "section", mode: "" }), "");
});

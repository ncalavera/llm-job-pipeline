import { test } from "node:test";
import assert from "node:assert/strict";
import { reviewBatches, batchConcern } from "./screen-batches.js";

const role = (id, title, extra = {}) => ({
  id,
  title,
  screening_state: "ready",
  status: "unseen",
  llm_score: null,
  screening: { posting_facts: { requirements: [] }, profile_comparison: [] },
  ...extra,
});
const status = (g) => g.status;

test("functional batches retain undecided roles exactly once without score cutoffs", () => {
  const product = role("p", "Technical Product Manager");
  const roles = [
    product,
    role("p2", "Product Manager", { llm_score: 1 }),
    role("o", "Head of Operations"),
    role("r", "Research Associate"),
    role("g", "Programme Manager"),
    role("f", "Fundraising Lead"),
    role("u", "Nurse"),
    role("mixed", "Product Manager, Operations"),
    role("declined", "Product Manager", { status: "declined" }),
    role("liked", "Product Manager", { status: "liked" }),
    role("failed", "Product Manager", { screening_state: "failed" }),
    product,
  ];
  const before = JSON.stringify(roles);
  const batches = reviewBatches(roles, status);
  assert.deepEqual(
    batches.map((b) => b.key),
    [
      "product",
      "operations",
      "research",
      "programmes",
      "partnerships",
      "other",
    ],
  );
  assert.deepEqual(
    batches[0].roles.map((g) => g.id),
    ["p2", "p"],
  );
  assert.equal(batches[0].roles[1], product);
  assert.equal(batches.flatMap((b) => b.roles).length, 8);
  assert.equal(JSON.stringify(roles), before);
  assert.deepEqual(reviewBatches([...roles].reverse(), status), batches);
  assert.deepEqual(
    reviewBatches(roles, () => "passed"),
    [],
  );
});

test("uses extracted function as fallback but does not override an explicit title", () => {
  const roles = [
    role("fallback", "Team lead", {
      screening: { posting_facts: { function: "Product management" } },
    }),
    role("title", "Chief of Staff", {
      screening: { posting_facts: { function: "Product management" } },
    }),
  ];
  const batches = reviewBatches(roles, status);
  assert.equal(batches[0].roles[0].id, "fallback");
  assert.equal(batches[1].roles[0].id, "title");
});

test("ranks by existing evidence without treating unknowns as conflicts or preferences as exclusions", () => {
  const evidence = (id, strength, finding, index = 0) =>
    role(id, "Product Manager", {
      screening: {
        posting_facts: {
          requirements: [{ strength, value: "Regional experience" }],
        },
        profile_comparison: [{ requirement: index, finding }],
      },
    });
  const conflict = evidence("conflict", "required", "possible_conflict");
  const roles = [
    conflict,
    role("unknown", "Product Manager"),
    evidence("match", "required", "match"),
    evidence("preferred", "preferred", "possible_conflict"),
    evidence("invalid", "required", "match", 7),
  ];
  assert.deepEqual(
    reviewBatches(roles, status)[0].roles.map((g) => g.id),
    ["match", "invalid", "preferred", "unknown", "conflict"],
  );
  assert.equal(
    batchConcern(conflict),
    "Check requirement: Regional experience",
  );
  assert.equal(batchConcern(roles[1]), "Profile fit still needs checking");
});

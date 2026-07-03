// pipeline.js — triage card rendering. buildTriageCard interpolates the private
// triage-review fields (cv_notes, skip_reason, research_question, …) into the
// card's innerHTML. Those are free text the user typed, so every one must be
// HTML-escaped before it reaches the DOM or a stored note like
// "<img src=x onerror=…>" becomes stored XSS (DHA-373). pipeline.js imports
// state.js (destructures window.VACANCY_DATA at eval) and Sortable, so we stub
// the browser globals those read at import time, then dynamic-import.

import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  VACANCY_DATA: {
    config: {},
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };

const { buildTriageCard } = await import("./pipeline.js");

// A non-compact column so the review-meta block renders (compact cards omit it).
const COL = { key: "to_apply", label: "To apply", color: "#000" };

function group() {
  return {
    id: "v1",
    org: "Example Org",
    title: "Some Role",
    locations: [],
    org_url: "",
  };
}

const XSS = '<img src=x onerror="alert(1)">';

test("buildTriageCard: a cv_note with <img onerror> renders inert, not a tag", () => {
  const html = buildTriageCard(group(), COL, { cv_notes: XSS });
  // No live tag and no live event-handler attribute may survive…
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /onerror="/); // the payload's quotes are escaped
  // …the note appears only as escaped, inert text instead.
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});

test("buildTriageCard: every private review field is HTML-escaped", () => {
  const fields = [
    "cv_notes",
    "skip_reason",
    "research_question",
    "network_contact",
    "deadline",
    "github_issue",
  ];
  for (const field of fields) {
    const html = buildTriageCard(group(), COL, { [field]: XSS });
    assert.doesNotMatch(html, /<img/i, `${field} leaked a raw <img> tag`);
    assert.match(html, /&lt;img/, `${field} was not escaped`);
  }
});

test("buildTriageCard: benign notes still render their text", () => {
  const html = buildTriageCard(group(), COL, {
    cv_notes: "Tailor CV for growth role",
  });
  assert.match(html, /Tailor CV for growth role/);
});

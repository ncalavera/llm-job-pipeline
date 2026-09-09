// =============================================================================
// review-filters.js — the "More filters" drawer.
//
// Three plain-word groups, no category codes, no jargon. Every control is a
// review aid: a filter narrows what you look at, it never decides for you, and
// "unknown" always stays in the list. Built ONCE at init (the option lists come
// from the whole payload, not from the filtered rows), so every control inside
// is a stable DOM node the seams in app.js can still address by id.
// =============================================================================

import { escHtml } from "./helpers.js";

function select(key, label, options) {
  return (
    '<label class="review-field"><span>' +
    escHtml(label) +
    '</span><select data-filter="' +
    escHtml(key) +
    '"' +
    '><option value="">' +
    "Any" +
    "</option>" +
    options
      .map(
        ([value, text]) =>
          '<option value="' +
          escHtml(value) +
          '">' +
          escHtml(text) +
          "</option>",
      )
      .join("") +
    "</select></label>"
  );
}

function text(key, label, placeholder) {
  return (
    '<label class="review-field"><span>' +
    escHtml(label) +
    '</span><input type="search" data-filter="' +
    escHtml(key) +
    '" placeholder="' +
    escHtml(placeholder) +
    '"></label>'
  );
}

function group(title, body) {
  return (
    '<section class="review-group"><h3>' +
    escHtml(title) +
    '</h3><div class="review-group-grid">' +
    body +
    "</div></section>"
  );
}

/**
 * The drawer body. `sources` and `orgs` are the distinct values in the payload;
 * `dates` are the first-seen days, newest first.
 */
export function reviewDrawerHtml({ sources = [], orgs = [], dates = [] } = {}) {
  return (
    group(
      "What the role asks for",
      select("kind", "Kind of requirement", [
        ["language", "Language"],
        ["authorisation", "Work permit"],
        ["location", "Location"],
        ["experience", "Years of experience"],
        ["education", "Degree"],
        ["skill", "Skill"],
        ["domain", "Field of work"],
        ["other", "Something else"],
      ]) +
        select("strength", "How strongly it is asked", [
          ["required", "Must have"],
          ["preferred", "Preferred"],
          ["unknown", "Only mentioned"],
        ]) +
        text(
          "requirementText",
          "Words in the requirement",
          "French, Portuguese, Africa…",
        ) +
        select("seniority", "Seniority", [
          ["junior", "Junior"],
          ["mid", "Mid"],
          ["senior", "Senior"],
          ["head", "Head"],
          ["director", "Director"],
          ["executive", "Executive"],
          ["unknown", "Not stated"],
        ]) +
        select("activity", "Kind of work", [
          ["building", "Building something new"],
          ["running", "Running something that exists"],
          ["selling", "Winning funding or partners"],
          ["specialist", "Specialist or research work"],
          ["unknown", "Not stated"],
        ]),
    ) +
    group(
      "Compared with my profile",
      select("finding", "Against your profile", [
        ["possible_conflict", "Possible conflict"],
        ["match", "Evidence of a match"],
        ["unknown", "Nothing recorded either way"],
      ]) +
        select("workMode", "Where the work happens", [
          ["remote", "Remote"],
          ["hybrid", "Hybrid"],
          ["onsite", "On site"],
          ["unknown", "Not stated"],
        ]),
    ) +
    group(
      "Where it came from and when",
      '<label class="review-field"><span>Company list</span><select id="catalogOrgFilter"><option value="">All companies</option>' +
        orgs
          .map(
            (o) =>
              '<option value="' + escHtml(o) + '">' + escHtml(o) + "</option>",
          )
          .join("") +
        "</select></label>" +
        select(
          "source",
          "Source",
          sources.map((s) => [s, s]),
        ) +
        select(
          "added",
          "Found",
          dates.map((d) => [d, d]),
        ) +
        select("age", "Time in the inbox", [
          ["last7", "Last 7 days"],
          ["last14", "Last 14 days"],
          ["last30", "Last 30 days"],
          ["older30", "Over 30 days"],
        ]) +
        '<label class="review-field"><span>Order the rows by</span><select id="reviewSort">' +
        '<option value="score-desc">Score, highest first</option>' +
        '<option value="score-asc">Score, lowest first</option>' +
        '<option value="deadline">Deadline, nearest first</option>' +
        "</select></label>",
    )
  );
}

/** Every drawer key screenMatches understands, so "Clear all" can reset them. */
export const DRAWER_KEYS = [
  "kind",
  "strength",
  "requirementText",
  "seniority",
  "activity",
  "finding",
  "workMode",
  "source",
  "added",
  "age",
];

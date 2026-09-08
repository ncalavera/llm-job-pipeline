// =============================================================================
// route.js — URL <-> route-object mapping (pure, dependency-free, unit-testable).
//
// The dashboard has two deep-linkable detail surfaces layered over the section
// list views: the company profile (?company=<slug>) and the vacancy detail page
// (?vacancy=<group-id>). Everything else is a plain section (a bare URL with no
// recognised param). This module is the single place that reads and writes those
// query strings, so app.js's popstate handler and cold-deep-link path share one
// parser and can never disagree on what a URL means.
//
// Route object shape (minimal):
//   { screen: "vacancy",  id: <group-id> }
//   { screen: "company",  id: <slug> }
//   { screen: "section" }                    // no detail overlay (bare URL)
//   { screen: "section", mode: "screen" }    // a leaf mode landed by URL
// `id` is present only for the two detail screens. A section route carries a
// `mode` only when the URL names one (?mode=screen, the digest's inbox link);
// app.js checks the mode against its leaf table before switching to it.
//
// Precedence: if a (malformed / hand-crafted) URL carries BOTH params, the
// vacancy route wins — it is the deeper, more specific target, and the app never
// emits both at once, so this only ever decides garbage input deterministically.
// =============================================================================

/**
 * Parse a location.search-style string into a route object. Accepts the value
 * with or without its leading "?". NEVER throws: malformed input, empty values,
 * and unknown params all collapse to the safe { screen: "section" } result.
 */
export function parse(search) {
  try {
    const params = new URLSearchParams(
      typeof search === "string" ? search : "",
    );
    // Vacancy wins over company if both are somehow present (see header note).
    const vacancy = params.get("vacancy");
    if (vacancy) return { screen: "vacancy", id: vacancy };
    const company = params.get("company");
    if (company) return { screen: "company", id: company };
    const mode = params.get("mode");
    if (mode) return { screen: "section", mode };
    return { screen: "section" };
  } catch (e) {
    return { screen: "section" };
  }
}

/**
 * Build the query string (including the leading "?") for a route object.
 * Only `screen`, `id` and `mode` are read; any other field is ignored. A detail route
 * with a missing/empty id, a section route, or anything unrecognised builds the
 * empty string (a bare URL). Round-trips with parse: build(parse(x)) is a
 * fixpoint for every valid x.
 */
export function build(route) {
  if (!route || typeof route !== "object") return "";
  if (route.screen === "vacancy" && route.id)
    return "?" + new URLSearchParams({ vacancy: String(route.id) }).toString();
  if (route.screen === "company" && route.id)
    return "?" + new URLSearchParams({ company: String(route.id) }).toString();
  if (route.screen === "section" && route.mode)
    return "?" + new URLSearchParams({ mode: String(route.mode) }).toString();
  return "";
}

// =============================================================================
// vacancy.js — Vacancy detail page (U6, DHA-390).
//
// The one detail surface for a role: a header band on the material (score tile,
// title, meta, actions) + a sheet split into a reading column (summary, model
// reasoning, description) and a facts rail (logistics, hard requirements, US
// warning, parent-company card). It renders ONLY from fields already in the
// group payload (R15) — the LLM-extracted structured sections in the mock are
// deferred.
//
// Split per KTD2: the assembly is pure, exported functions (facts rail, action
// gating, auto-advance queue pick, page HTML) that unit-test under node --test;
// the DOM shell (renderVacancyDetail + the like/pass/apply handlers) is thin
// and wires them to state + the U4 router. Every externally-sourced string
// (description, reasoning, title, org, location) goes through escHtml and every
// href through safeUrl before innerHTML (R14).
// =============================================================================

import {
  state,
  groupsById,
  archivedGroupsById,
  getCompanies,
  getGroupStatus,
  STATUS_BASKET,
  updateStatus,
} from "./state.js";
import {
  escHtml,
  jsAttr,
  safeUrl,
  mdToHtml,
  relativeTime,
  formatDeadlineHtml,
  qualityBand,
  tierClass,
  isVacancyExpired,
  isVacancyStale,
  resolveVacancyCompany,
} from "./helpers.js";
import { T, dateLocale } from "./i18n.js";

// ---------------------------------------------------------------------------
// Pure assembly — no DOM, no state; unit-tested directly.
// ---------------------------------------------------------------------------

// Friendly name for a company's fetch strategy (its ATS / board), used as the
// facts-rail "Source" row. manual_check / unknown / absent → "" so the row is
// omitted (AE1).
const SOURCE_LABELS = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  workable: "Workable",
  recruitee: "Recruitee",
  teamtailor_rss: "Teamtailor",
  bamboohr: "BambooHR",
  workday_api: "Workday",
  amazon_jobs: "Amazon Jobs",
  unops_widget: "UNOPS",
  firecrawl_scrape: "Company site",
};

export function sourceLabel(strategy) {
  if (!strategy || strategy === "manual_check") return "";
  return (
    SOURCE_LABELS[strategy] ||
    strategy.charAt(0).toUpperCase() + strategy.slice(1)
  );
}

// Which action buttons the page shows for a live status. Mirrors the catalog
// card's per-basket thumb logic (unseen → like+pass, liked → pass, passed →
// like) and adds the primary "Move to apply", hidden once the role is already
// in/through apply so the CTA never contradicts the status chip. Research /
// Network are the two remaining triage dispositions (both valid /api/save
// statuses) surfaced here too, each hidden once the role already sits in that
// exact status or has been applied to. An archived role is a read-only page
// (reached from the Archive list) - no decision buttons, only Open posting.
export function vacancyActions(status) {
  if (status === "archived") {
    return {
      canLike: false,
      canPass: false,
      canApply: false,
      canResearch: false,
      canNetwork: false,
    };
  }
  const basket = STATUS_BASKET[status] || "unseen";
  return {
    canLike: basket === "unseen" || basket === "passed",
    canPass: basket === "unseen" || basket === "liked",
    canApply: status !== "to_apply" && status !== "applied",
    canResearch: status !== "to_research" && status !== "applied",
    canNetwork: status !== "to_network" && status !== "applied",
  };
}

// Header status chip so a change confirms in place (F3/R18, non-Browse entry).
// `unseen` (nothing decided) and `expiring` (shown as its own badge) get none.
const STATUS_CHIP_KEYS = {
  liked: ["vac_status_liked", "Liked"],
  passed: ["vac_status_passed", "Passed"],
  skipped: ["vac_status_passed", "Passed"],
  to_apply: ["vac_status_to_apply", "To apply"],
  to_research: ["vac_status_to_research", "Research"],
  to_network: ["vac_status_to_network", "Networking"],
  applied: ["vac_status_applied", "Applied"],
  archived: ["vac_status_archived", "Archived"],
};

export function statusChipLabel(status, t) {
  const translate = t || ((k, fb) => fb);
  const entry = STATUS_CHIP_KEYS[status];
  return entry ? translate(entry[0], entry[1]) : null;
}

// A UTC-pinned calendar date, matching formatDeadlineHtml's date handling, so
// the rail's "First seen" reads as an absolute date (the header carries the
// relative "seen 3d ago"). Blank/invalid → "".
function fmtDate(raw, locale) {
  if (!raw) return "";
  const d = new Date(String(raw).slice(0, 10));
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(locale || "en-US", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

// Assemble the facts rail from ONLY the fields present on the group (AE1: an
// absent field yields no row, never an empty label). Values are HTML-safe:
// external strings run through escHtml; the deadline reuses formatDeadlineHtml
// (which escapes its own copy). `company` supplies the Source row. Returns
// { facts:[{label,value}], locations:[{text,url}] }.
export function buildFactsRail(g, company, opts) {
  const o = opts || {};
  const t = o.t || ((k, fb) => fb);
  const locale = o.locale || "en-US";
  const facts = [];

  if (g.compensation) {
    facts.push({
      label: t("vac_comp", "Compensation"),
      value: escHtml(g.compensation),
    });
  }
  if (g.deadline) {
    const dl = formatDeadlineHtml(g.deadline, "vac-fact-deadline", {
      t,
      locale,
    });
    if (dl) facts.push({ label: t("vac_deadline", "Deadline"), value: dl });
  }
  if (g.first_seen) {
    const d = fmtDate(g.first_seen, locale);
    if (d)
      facts.push({
        label: t("vac_first_seen", "First seen"),
        value: escHtml(d),
      });
  }
  const src = company ? sourceLabel(company.strategy) : "";
  if (src)
    facts.push({ label: t("vac_source", "Source"), value: escHtml(src) });

  // The application entity's own status (draft/applied/interview/offer/
  // rejected/withdrawn — scripts/applications.py VALID_STATUSES) is a finer
  // lifecycle than the vacancy's coarse review status (STATUS_CHIP_KEYS has no
  // entry for interview/offer/rejected/withdrawn/draft at all), so the header
  // status chip alone can't show it. Relocated from the retired Browse card's
  // "✉ applied" badge (U5 parity) — same raw status text, applied_at now
  // formatted via fmtDate instead of a hover-only tooltip.
  if (g.application && g.application.status) {
    const appliedDate = g.application.applied_at
      ? fmtDate(g.application.applied_at, locale)
      : "";
    facts.push({
      label: t("application_marker", "Application"),
      value:
        escHtml(g.application.status) +
        (appliedDate ? " · " + escHtml(appliedDate) : ""),
    });
  }

  // safeUrl validates the scheme but does NOT escape quotes; escHtml here so the
  // value is safe to drop straight into href="…" (R14 — matches catalog.js:299,
  // today.js:142). "" (unsafe/absent) stays falsy for the link/plain branch.
  const locations = (g.locations || [])
    .filter((l) => l && l.location)
    .map((l) => ({
      text: escHtml(l.location),
      url: escHtml(safeUrl(l.url || "")),
    }));

  return { facts, locations };
}

// The next vacancy to advance to after "Move to apply" from a Browse-unreviewed
// context (F3). `queue` is the ordered id list the user was looking at; the
// predicate reports whether an id is STILL unreviewed now (recomputed live, so
// ids actioned since entry are skipped — AE5's id-keyed spirit). Scans forward
// from currentId (from the start when it's no longer in the queue). Returns
// null when none remain → the caller shows the done banner.
export function nextUnreviewedId(currentId, queue, isUnseen) {
  if (!Array.isArray(queue) || queue.length === 0) return null;
  const at = queue.indexOf(currentId);
  const start = at === -1 ? 0 : at + 1;
  for (let i = start; i < queue.length; i++) {
    if (queue[i] !== currentId && isUnseen(queue[i])) return queue[i];
  }
  return null;
}

// The id one step (±1) away from `currentId` in the entry queue (post-ship
// fast fix: ← → / k j browse the SAME list the page was opened from, unlike
// nextUnreviewedId above which only advances through still-unreviewed rows
// after "Move to apply"). Clamped, not wrapped — mirrors keys.js's cursor
// move. Returns null when there's no queue, currentId isn't in it, or the
// step would go past an end (the caller no-ops).
export function queueStep(currentId, queue, delta) {
  if (!Array.isArray(queue) || queue.length === 0) return null;
  const at = queue.indexOf(currentId);
  if (at === -1) return null;
  const next = at + delta;
  if (next < 0 || next >= queue.length) return null;
  return queue[next];
}

// First one or two initials of a company name, for the mini-card monogram.
function initials(name) {
  return String(name || "")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0] || "")
    .join("")
    .toUpperCase();
}

function label(text) {
  return '<div class="vac-section-label">' + escHtml(text) + "</div>";
}

// Quiet mono keyboard-hint row (post-ship fast fix), same recipe as Browse's
// .browse-keyhint: ← →/k j walk the entry queue (a no-op without one — a cold
// deep link, or an entry point that carries no queue), Esc always goes back.
// Shown unconditionally, same as Browse's j/k/l/x hint showing even before a
// row is selected — Esc alone is always live.
function vacancyKeyhintHtml(t) {
  return (
    '<div class="vac-keyhint">' +
    '<span class="vac-key">←</span><span class="vac-key">→</span>' +
    '<span class="vac-keyhint-label">' +
    escHtml(t("vac_keyhint_browse", "browse")) +
    "</span>" +
    '<span class="vac-key">l</span>' +
    '<span class="vac-keyhint-label">' +
    escHtml(t("vac_keyhint_like", "like")) +
    "</span>" +
    '<span class="vac-key">x</span>' +
    '<span class="vac-keyhint-label">' +
    escHtml(t("vac_keyhint_pass", "pass")) +
    "</span>" +
    '<span class="vac-key">Esc</span>' +
    '<span class="vac-keyhint-label">' +
    escHtml(t("vac_keyhint_back", "back")) +
    "</span>" +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Pure page HTML — takes the group, its parent company (or null), the live
// status, and { t, locale }. No DOM/state read, so it renders identically in a
// test as in the browser and the escaping-regression suite can assert on it.
// ---------------------------------------------------------------------------

export function vacancyPageHtml(g, company, status, opts) {
  const o = opts || {};
  const t = o.t || ((k, fb) => fb);
  const locale = o.locale || "en-US";

  const score = g.llm_score;
  const scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";
  const scoreTxt = score == null ? "—" : String(score);

  const idAttr = jsAttr(g.id);
  const acts = vacancyActions(status);

  // --- Header band ---
  const [orgFg] = g.org_color || ["#8A867C", "#F6F8FA"];
  const orgName = escHtml(g.company_name || g.org);
  // company (not g.company_slug, a dead field — see resolveVacancyCompany)
  // decides both the header link and the tier badge below (post-ship fast
  // fix #6): an org with no matching company entry renders as plain text,
  // never a dead click target.
  const orgHtml = company
    ? '<span class="vac-org vac-org--link" role="button" tabindex="0" onclick="openCompanyProfile(\'' +
      jsAttr(company.slug) +
      "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openCompanyProfile('" +
      jsAttr(company.slug) +
      "')}\">" +
      orgName +
      "</span>"
    : '<span class="vac-org">' + orgName + "</span>";

  const tierHtml =
    company && company.calculated_tier
      ? '<span class="vac-tier ' +
        tierClass(company.calculated_tier) +
        '">' +
        escHtml(company.calculated_tier) +
        "</span>"
      : "";

  const primaryLoc = (g.locations || []).find((l) => l && l.location);
  const locHtml = primaryLoc
    ? '<span class="vac-meta-loc">' + escHtml(primaryLoc.location) + "</span>"
    : "";
  const compMeta = g.compensation
    ? '<span class="vac-meta-comp">' + escHtml(g.compensation) + "</span>"
    : "";
  const seenHtml = g.first_seen
    ? '<span class="vac-meta-seen">' +
      escHtml(t("vac_seen_prefix", "seen")) +
      " " +
      escHtml(relativeTime(g.first_seen, t)) +
      "</span>"
    : "";

  // Expiring/expired badge (deadline-derived) takes the crimson slot; the
  // status chip covers decision statuses only.
  let stateBadge = "";
  if (status === "expiring") {
    stateBadge =
      '<span class="vac-badge vac-badge--expiring">' +
      escHtml(t("vac_status_expiring", "Expiring")) +
      "</span>";
  } else if (isVacancyExpired(g)) {
    stateBadge =
      '<span class="vac-badge vac-badge--expired">' +
      escHtml(t("vac_status_expired", "Expired")) +
      "</span>";
  }
  // Source-freshness warning (relocated from the retired Browse card, U5
  // parity): the source hasn't confirmed this role in STALE_SOURCE_DAYS+ days
  // (same isVacancyStale derivation Triage's "Expired" column uses). Only the
  // negative case is worth a badge (AE1) — a fresh role needs no callout.
  const staleBadge = isVacancyStale(g)
    ? '<span class="vac-badge vac-badge--stale" title="' +
      escHtml(
        t(
          "freshness_stale_hint",
          "based on the last time the source confirmed the role; direct ATS is exact, aggregators approximate",
        ),
      ) +
      '">' +
      escHtml(t("freshness_stale", "stale, likely closed")) +
      "</span>"
    : "";
  const chipText = statusChipLabel(status, t);
  const statusChip = chipText
    ? '<span class="vac-status-chip">' + escHtml(chipText) + "</span>"
    : "";

  const primaryUrl = safeUrl(
    (g.locations || []).find((l) => l && l.url)?.url || g.org_url || "",
  );
  const openPosting = primaryUrl
    ? '<a class="vac-btn vac-btn--link" href="' +
      escHtml(primaryUrl) +
      '" target="_blank" rel="noopener">' +
      escHtml(t("vac_open_posting", "Open posting")) +
      " ↗</a>"
    : "";
  const applyBtn = acts.canApply
    ? '<button class="vac-btn vac-btn--apply" onclick="vacancyMoveToApply(\'' +
      idAttr +
      "')\">" +
      escHtml(t("vac_move_to_apply", "Move to apply")) +
      "</button>"
    : "";
  const likeBtn = acts.canLike
    ? '<button class="vac-btn vac-btn--like" onclick="vacancyLike(\'' +
      idAttr +
      "')\">✓ " +
      escHtml(t("vac_like", "Like")) +
      "</button>"
    : "";
  const passBtn = acts.canPass
    ? '<button class="vac-btn vac-btn--pass" onclick="vacancyPass(\'' +
      idAttr +
      "')\">✕ " +
      escHtml(t("vac_pass", "Pass")) +
      "</button>"
    : "";
  // Research / Network: the two triage dispositions that used to be reachable
  // only from the Triage board. Same inline-onclick + status path as the other
  // action buttons; gated by vacancyActions above.
  const researchBtn = acts.canResearch
    ? '<button class="vac-btn vac-btn--research" onclick="vacancyResearch(\'' +
      idAttr +
      "')\">" +
      escHtml(t("vac_research", "Research")) +
      "</button>"
    : "";
  const networkBtn = acts.canNetwork
    ? '<button class="vac-btn vac-btn--network" onclick="vacancyNetwork(\'' +
      idAttr +
      "')\">" +
      escHtml(t("vac_network", "Network")) +
      "</button>"
    : "";

  const header =
    '<div class="vac-header">' +
    '<button class="vac-back" onclick="closeDetail()">← ' +
    escHtml(t("vac_back", "Back")) +
    "</button>" +
    '<div class="vac-header-main">' +
    '<div class="vac-score-tile ' +
    scoreCls +
    '">' +
    escHtml(scoreTxt) +
    "</div>" +
    '<div class="vac-header-text">' +
    '<div class="vac-title-row">' +
    '<h1 class="vac-title">' +
    escHtml(g.title) +
    "</h1>" +
    stateBadge +
    staleBadge +
    statusChip +
    "</div>" +
    '<div class="vac-meta">' +
    orgHtml +
    tierHtml +
    locHtml +
    compMeta +
    seenHtml +
    "</div>" +
    "</div>" +
    '<div class="vac-actions">' +
    applyBtn +
    researchBtn +
    networkBtn +
    likeBtn +
    passBtn +
    openPosting +
    "</div>" +
    "</div>" +
    "</div>";

  // --- Reading column ---
  const summaryText = g.llm_summary || g.snippet || "";
  const readParts = [];
  if (summaryText) {
    readParts.push(
      '<div class="vac-summary">' + escHtml(summaryText) + "</div>",
    );
  }
  if (g.llm_reasoning) {
    readParts.push(
      '<div class="vac-reasoning">' +
        '<div class="vac-caption">' +
        escHtml(t("vac_reasoning", "Model reasoning")) +
        (score == null ? "" : " · " + escHtml(String(score))) +
        "</div>" +
        '<div class="vac-reasoning-text">' +
        escHtml(g.llm_reasoning) +
        "</div>" +
        "</div>",
    );
  }
  // The full description only when it adds meaningfully beyond the summary
  // (same gate the catalog card used before it was retired), rendered as
  // readable prose through mdToHtml (which escapes before formatting). The
  // LLM summary already leads the reading column in the profile's product
  // language (DHA-411); the original source text — often English regardless
  // of that language — sits behind a collapsed <details>, same pattern as
  // the company page's evidence/deep-analysis blocks.
  if (
    g.full_description &&
    g.full_description.length > summaryText.length + 50
  ) {
    readParts.push(
      '<details class="vac-desc-block"><summary class="vac-section-label">' +
        escHtml(t("vac_description", "About the role")) +
        "</summary>" +
        '<div class="vac-desc">' +
        mdToHtml(g.full_description) +
        "</div>" +
        "</details>",
    );
  }
  if (readParts.length === 0) {
    readParts.push(
      '<div class="vac-empty">' +
        escHtml(t("vac_empty_body", "No description available yet.")) +
        "</div>",
    );
  }
  const reading = '<div class="vac-reading">' + readParts.join("") + "</div>";

  // --- Facts rail ---
  const rail = buildFactsRail(g, company, { t, locale });
  const railParts = [];
  if (rail.facts.length) {
    railParts.push(
      '<div class="vac-rail-group">' +
        label(t("vac_facts", "Facts")) +
        rail.facts
          .map(
            (f) =>
              '<div class="vac-fact"><span class="vac-fact-k">' +
              escHtml(f.label) +
              '</span><span class="vac-fact-v">' +
              f.value +
              "</span></div>",
          )
          .join("") +
        "</div>",
    );
  }
  if (rail.locations.length) {
    railParts.push(
      '<div class="vac-rail-group">' +
        label(t("vac_locations", "Locations")) +
        rail.locations
          .map((l) =>
            l.url
              ? '<a class="vac-loc vac-loc--link" href="' +
                l.url +
                '" target="_blank" rel="noopener">' +
                l.text +
                " ↗</a>"
              : '<div class="vac-loc">' + l.text + "</div>",
          )
          .join("") +
        "</div>",
    );
  }
  if (g.us_eligibility === "unclear") {
    railParts.push(
      '<div class="vac-us-warn">⚠ ' +
        escHtml(t("vac_us_warning", "US work eligibility unclear")) +
        "</div>",
    );
  }
  const reqs = g.llm_hard_requirements || [];
  if (reqs.length) {
    railParts.push(
      '<div class="vac-rail-group">' +
        label(t("vac_requirements", "Hard requirements")) +
        '<div class="vac-req-chips">' +
        reqs
          .map((r) => '<span class="vac-req-chip">' + escHtml(r) + "</span>")
          .join("") +
        "</div>" +
        "</div>",
    );
  }
  if (company) {
    const note = company.sector || company.description || "";
    railParts.push(
      '<div class="vac-company-card" role="button" tabindex="0" onclick="openCompanyProfile(\'' +
        jsAttr(company.slug) +
        "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openCompanyProfile('" +
        jsAttr(company.slug) +
        "')}\">" +
        '<div class="vac-company-top">' +
        '<span class="vac-company-mono" style="background:' +
        escHtml(orgFg) +
        '">' +
        escHtml(initials(company.name)) +
        "</span>" +
        '<span class="vac-company-name">' +
        escHtml(company.name) +
        "</span>" +
        (company.calculated_tier
          ? '<span class="vac-tier ' +
            tierClass(company.calculated_tier) +
            '">' +
            escHtml(company.calculated_tier) +
            "</span>"
          : "") +
        "</div>" +
        (note
          ? '<div class="vac-company-note">' + escHtml(note) + "</div>"
          : "") +
        '<div class="vac-company-link">' +
        escHtml(t("vac_view_company", "View company")) +
        " →</div>" +
        "</div>",
    );
  }
  const railHtml = '<aside class="vac-rail">' + railParts.join("") + "</aside>";

  return (
    '<div class="vac-page">' +
    header +
    vacancyKeyhintHtml(t) +
    '<div class="vac-sheet">' +
    reading +
    railHtml +
    "</div>" +
    "</div>"
  );
}

// Fixed, parameter-free not-found panel (missing id / vacancy gone after a
// poll). The raw id is never interpolated (R14 / AE6). Back routes to Browse.
export function vacancyNotFoundHtml(opts) {
  const t = (opts && opts.t) || ((k, fb) => fb);
  return (
    '<div class="vac-page"><div class="catalog-empty">' +
    '<div class="catalog-empty-icon">🔍</div>' +
    "<strong>" +
    escHtml(t("route_not_found", "Not found")) +
    "</strong>" +
    '<div class="catalog-empty-hint">' +
    escHtml(
      t("route_not_found_hint", "This page doesn’t exist or hasn’t loaded."),
    ) +
    "</div>" +
    '<div class="vac-empty-actions"><button class="vac-btn vac-btn--apply" onclick="switchVacancies()">' +
    escHtml(t("route_back", "Go back")) +
    "</button></div>" +
    "</div></div>"
  );
}

// Terminal banner when the Browse-unreviewed queue drains (F3).
export function vacancyQueueDoneHtml(opts) {
  const t = (opts && opts.t) || ((k, fb) => fb);
  return (
    '<div class="vac-page"><div class="catalog-empty">' +
    '<div class="catalog-empty-icon">✅</div>' +
    "<strong>" +
    escHtml(t("vac_queue_done", "All caught up")) +
    "</strong>" +
    '<div class="catalog-empty-hint">' +
    escHtml(
      t("vac_queue_done_hint", "No more unreviewed roles in this list."),
    ) +
    "</div>" +
    '<div class="vac-empty-actions"><button class="vac-btn vac-btn--apply" onclick="switchVacancies()">' +
    escHtml(t("vac_back_to_browse", "Back to Browse")) +
    "</button></div>" +
    "</div></div>"
  );
}

// ---------------------------------------------------------------------------
// DOM shell — thin: look up the group, pick the HTML, set innerHTML. Actions
// use the inline-onclick pattern (KTD4) and the existing status path.
// ---------------------------------------------------------------------------

function pageOpts() {
  return { t: T, locale: dateLocale() };
}

// Render the vacancy detail into #vacancyDetail (called by app.js's seam:
// openVacancyRoute, popstate, cold deep link, and the "render" handler). A
// missing/gone id renders the fixed not-found state instead of flashing it for
// a valid deep link (the U4 placeholder always showed not-found).
export function renderVacancyDetail(id) {
  const host = document.getElementById("vacancyDetail");
  if (!host) return;
  let g = groupsById.get(id);
  let status;
  if (g) {
    status = getGroupStatus(g);
  } else {
    // Archived rows aren't in groupsById (kept out so they can't skew company
    // rollups - see state.js archivedGroupsById). Resolve the id there so an
    // Archive-list click opens a real, read-only page instead of "not found".
    g = archivedGroupsById.get(id);
    if (!g) {
      host.innerHTML = vacancyNotFoundHtml(pageOpts());
      return;
    }
    status = "archived";
  }
  // g.company_slug is a dead field (data_prep.py never sets it) — resolve the
  // parent company by the real shared key instead (post-ship fast fix #6).
  const company = resolveVacancyCompany(g, getCompanies());
  host.innerHTML = vacancyPageHtml(g, company, status, pageOpts());
}

// After a verdict from a Browse-unreviewed entry, hop to the next STILL-
// unreviewed role in the same queue (F3) — the same advance "Move to apply"
// uses, now shared by like/pass so any verdict walks you forward. Other entry
// points (Today, company roles, cold deep link) confirm in place: the
// statusChanged re-render updates the actions + status chip and the toast
// fires. Returns true when it took over the page (advanced or showed the
// queue-done banner), false when it left the page for the caller's re-render.
function advanceBrowseQueue(id) {
  const entry = state.vacancyEntry;
  if (!entry || entry.context !== "browse") return false;

  const isUnseen = (vid) => {
    const vg = groupsById.get(vid);
    return vg
      ? (STATUS_BASKET[getGroupStatus(vg)] || "unseen") === "unseen"
      : false;
  };
  const next = nextUnreviewedId(id, entry.queue, isUnseen);
  if (next) {
    // replace (not push) so the whole advance chain is one history entry: Back
    // from any hop returns straight to the originating Browse list (F1).
    window.openVacancyRoute(next, {
      context: "browse",
      queue: entry.queue,
      replace: true,
    });
    return true;
  }
  // Queue drained — terminal done banner. Clear the id so the scheduled
  // "render" won't repaint the page over the banner.
  state.currentVacancyId = null;
  state.vacancyEntry = null;
  const host = document.getElementById("vacancyDetail");
  if (host) host.innerHTML = vacancyQueueDoneHtml(pageOpts());
  return true;
}

export function vacancyLike(id) {
  const g = groupsById.get(id);
  if (!g) return;
  updateStatus(id, g.member_ids || [], "liked");
  advanceBrowseQueue(id);
}

export function vacancyPass(id) {
  const g = groupsById.get(id);
  if (!g) return;
  updateStatus(id, g.member_ids || [], "passed");
  advanceBrowseQueue(id);
}

export function vacancyResearch(id) {
  const g = groupsById.get(id);
  if (g) updateStatus(id, g.member_ids || [], "to_research");
}

export function vacancyNetwork(id) {
  const g = groupsById.get(id);
  if (g) updateStatus(id, g.member_ids || [], "to_network");
}

export function vacancyMoveToApply(id) {
  const g = groupsById.get(id);
  if (!g) return;
  updateStatus(id, g.member_ids || [], "to_apply");
  advanceBrowseQueue(id);
}

// ---------------------------------------------------------------------------
// Keyboard nav (post-ship fast fix). One document-level listener, same
// discipline as catalog.js's browseKeydown: returns early unless the vacancy
// page is the active, in-focus surface with no palette open. Escape always
// goes back; ←/→ (also k/j) step through state.vacancyEntry.queue — the SAME
// queue "Move to apply" auto-advances through above — and no-op gracefully
// when there isn't one (cold deep link, or an entry point that carries none).
// ---------------------------------------------------------------------------

function vacancyKeydown(e) {
  if (e.isComposing) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  const active = document.activeElement;
  if (active) {
    const tag = active.tagName;
    if (
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      active.isContentEditable
    )
      return;
  }

  const palette = document.getElementById("commandPalette");
  if (palette && palette.classList.contains("active")) return;

  const host = document.getElementById("vacancyDetail");
  if (!host || !host.classList.contains("active")) return;

  const key = e.key;

  if (key === "Escape") {
    e.preventDefault();
    if (typeof window.closeDetail === "function") window.closeDetail();
    return;
  }

  // l / x mirror Browse's like/pass on the open vacancy — gated by the SAME
  // vacancyActions the Like/Pass buttons render from, so a status that shows no
  // button (to_apply, applied, archived, …) makes the key a no-op too.
  if (key === "l" || key === "x") {
    const vid = state.currentVacancyId;
    if (!vid) return;
    const g = groupsById.get(vid);
    if (!g) return;
    const acts = vacancyActions(getGroupStatus(g));
    if (key === "l" && acts.canLike) {
      e.preventDefault();
      vacancyLike(vid);
    } else if (key === "x" && acts.canPass) {
      e.preventDefault();
      vacancyPass(vid);
    }
    return;
  }

  if (key !== "ArrowLeft" && key !== "ArrowRight" && key !== "k" && key !== "j")
    return;

  const id = state.currentVacancyId;
  const entry = state.vacancyEntry;
  if (!id || !entry || !entry.queue || !entry.queue.length) return;

  const delta = key === "ArrowRight" || key === "j" ? 1 : -1;
  const next = queueStep(id, entry.queue, delta);
  if (!next) return;
  e.preventDefault();
  // replace (not push), same as "Move to apply" auto-advance: Back from any
  // hop returns straight to the originating list, not step-by-step.
  window.openVacancyRoute(next, {
    context: entry.context,
    queue: entry.queue,
    replace: true,
  });
}

if (typeof document !== "undefined") {
  document.addEventListener("keydown", vacancyKeydown);
}

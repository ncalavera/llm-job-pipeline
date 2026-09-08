// =============================================================================
// settings.js — Settings section: RESOLVED config dials, read-only.
//
// The generator (scripts/report/data_prep.py prepare_settings_payload) bakes the
// values in effect right now — the [volume] dials, the scoring model tier and
// the thresholds — each with a neutral `source` pointer (a file + section). No
// editing UI this wave: the point is one screen where a stranger can see what's
// configured and the exact line to change. RESOLVED VALUES ONLY reach here —
// never personal profile prose, secrets or artifact values. Values render in
// neutral mono (not cobalt): they aren't clickable in this UI, and the design
// protocol reserves cobalt for actual interaction (design-protocol.md #1).
// =============================================================================

import { escHtml } from "./helpers.js";
import { T } from "./i18n.js";

function _valueCell(v) {
  if (v === null || v === undefined || v === "") return "—";
  return escHtml(String(v));
}

function _row(r) {
  return (
    '<div class="stg-row">' +
    '<span class="stg-label">' +
    escHtml(T(r.key, r.key)) +
    "</span>" +
    '<span class="stg-value"><code>' +
    _valueCell(r.value) +
    "</code></span>" +
    '<span class="stg-source"><code>' +
    escHtml(r.source || "") +
    "</code></span>" +
    "</div>"
  );
}

function _group(g) {
  return (
    '<section class="stg-group">' +
    '<div class="stg-group-title">' +
    escHtml(T(g.key, g.key)) +
    "</div>" +
    (g.rows || []).map(_row).join("") +
    "</section>"
  );
}

export function renderSettings() {
  const root = document.getElementById("settingsSection");
  if (!root) return;

  const data = (window.VACANCY_DATA && window.VACANCY_DATA.settings) || null;
  const groups = (data && data.groups) || [];

  const header =
    '<div class="settings-header">' +
    '<span class="settings-header-title">' +
    escHtml(T("settings_title", "Settings")) +
    "</span>" +
    '<span class="settings-header-sub">' +
    escHtml(
      T(
        "settings_sub",
        "Resolved values in effect right now. Read-only — each row shows the one line to change.",
      ),
    ) +
    "</span>" +
    "</div>";

  const glossary = '<details class="stg-group"><summary>' + escHtml(T("concepts_title", "Terms and states")) + '</summary>' + [
    ["concepts_entities", "Company: the organisation offering a vacancy. Job board: a site publishing vacancies from many companies. Source: the board or careers site where a posting was collected."],
    ["concepts_tracking", "Company tracking: To review / Tracked / Not tracked. Board collection: Enabled / Disabled. Board visibility: Shown / Hidden. Hiding does not stop collection."],
    ["concepts_connection", "Connection: Automatic / Manual / Not connected. Last check: Never checked / Succeeded / Failed. A successful check can find zero vacancies. Overdue means the next check is late."],
    ["concepts_preparation", "Facts quote the posting. Fit compares those facts with your profile. Preparation: Not prepared / Needs update / Ready / Failed. Only current, ready, undecided vacancies enter Inbox."],
    ["concepts_decision", "Decision: Undecided / Kept / Passed. After Keep, choose a next step: Prepare application / Research company / Contact someone. Archive is storage history, not your decision to Pass."],
    ["concepts_application", "Application stages: Draft / Applied / Test task / Interview / Offer received / Rejected by employer / Withdrawn. Offer received means the employer said yes; it does not mean you accepted."],
    ["concepts_availability", "Availability: Deadline passed / Not recently confirmed / No closure signal. These signals never erase your decision or application."],
    ["concepts_contacts", "Contact: To contact / Awaiting reply / Replied / Met / Declined contact / No longer following up. These describe a conversation, not an application."],
    ["concepts_counts", "Each count belongs to its named list. Function groups divide Inbox; attribute filters may overlap. Scores are optional numerical estimates, not Facts, Fit, or Decisions. Reports are research documents; the daily update is just the inbox count and link."],
  ].map(([key, text]) => '<p>' + escHtml(T(key, text)) + '</p>').join('') + '</details>';

  if (!groups.length) {
    root.innerHTML = header + glossary + '<div class="stg-sheet stg-empty">—</div>';
    return;
  }

  root.innerHTML =
    header + glossary + '<div class="stg-sheet">' + groups.map(_group).join("") + "</div>";
}

export function initSettings() {
  renderSettings();
}

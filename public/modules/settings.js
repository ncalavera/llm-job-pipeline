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

  if (!groups.length) {
    root.innerHTML = header + '<div class="stg-sheet stg-empty">—</div>';
    return;
  }

  root.innerHTML =
    header + '<div class="stg-sheet">' + groups.map(_group).join("") + "</div>";
}

export function initSettings() {
  renderSettings();
}

// =============================================================================
// settings.js — Settings section: RESOLVED config dials, read-only.
//
// The generator (scripts/report/data_prep.py prepare_settings_payload) bakes the
// values in effect right now — the [volume] dials, the scoring model tier and
// the thresholds — each with a neutral `source` pointer (a file + section). No
// editing UI this wave: the point is one screen where a stranger can see what's
// configured and the exact line to change. RESOLVED VALUES ONLY reach here —
// never personal profile prose, secrets or artifact values.
// =============================================================================

import { escHtml } from "./helpers.js";
import { T } from "./i18n.js";

function _valueCell(v) {
  if (v === null || v === undefined || v === "") return "—";
  return escHtml(String(v));
}

function _row(r) {
  return (
    '<tr class="settings-row">' +
    '<td class="settings-td settings-td-label">' +
    escHtml(T(r.key, r.key)) +
    "</td>" +
    '<td class="settings-td settings-td-value"><code>' +
    _valueCell(r.value) +
    "</code></td>" +
    '<td class="settings-td settings-td-source"><code>' +
    escHtml(r.source || "") +
    "</code></td>" +
    "</tr>"
  );
}

function _group(g) {
  const head =
    "<thead><tr>" +
    "<th>" +
    escHtml(T("settings_col_setting", "Setting")) +
    "</th>" +
    "<th>" +
    escHtml(T("settings_col_value", "Value")) +
    "</th>" +
    "<th>" +
    escHtml(T("settings_col_source", "Where to change")) +
    "</th>" +
    "</tr></thead>";
  return (
    '<section class="settings-group">' +
    '<h3 class="settings-group-title">' +
    escHtml(T(g.key, g.key)) +
    "</h3>" +
    '<table class="settings-table">' +
    head +
    "<tbody>" +
    (g.rows || []).map(_row).join("") +
    "</tbody></table>" +
    "</section>"
  );
}

export function renderSettings() {
  const root = document.getElementById("settingsSection");
  if (!root) return;

  const data = (window.VACANCY_DATA && window.VACANCY_DATA.settings) || null;
  const groups = (data && data.groups) || [];

  const intro =
    '<div class="settings-intro">' +
    '<h2 class="settings-h2">⚙️ <span>' +
    escHtml(T("settings_title", "Settings")) +
    "</span></h2>" +
    '<p class="settings-sub">' +
    escHtml(
      T(
        "settings_sub",
        "Resolved values in effect right now. Read-only — each row shows the one line to change.",
      ),
    ) +
    "</p></div>";

  if (!groups.length) {
    root.innerHTML = intro + '<div class="settings-empty">—</div>';
    return;
  }

  root.innerHTML = intro + groups.map(_group).join("");
}

export function initSettings() {
  renderSettings();
}

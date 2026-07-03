// settings.js — the read-only Settings section. It only reads
// window.VACANCY_DATA.settings directly, but its i18n.js import pulls in
// state.js transitively, which destructures the full VACANCY_DATA shape at
// module load time — so the shim needs the same fields archive.test.js/
// boards.test.js seed, even though this module never touches most of them.

import { test } from "node:test";
import assert from "node:assert/strict";

const root = { innerHTML: "" };
const byId = { settingsSection: root };

const settingsData = { groups: [] };

globalThis.document = {
  getElementById: (id) => byId[id] || null,
};
globalThis.window = {
  VACANCY_DATA: {
    config: { i18n: {}, i18n_all: null, language: "en" },
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
    settings: settingsData,
  },
};
globalThis.location = { protocol: "file:", origin: "" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const { renderSettings } = await import("./settings.js");

function setGroups(groups) {
  settingsData.groups = groups;
}

test("renders every group with its label/value/source rows", () => {
  setGroups([
    {
      key: "settings_grp_volume",
      rows: [
        {
          key: "set_max_active_companies",
          value: 40,
          source: "config/defaults.toml → [volume]",
        },
        {
          key: "set_digest_size",
          value: 10,
          source: "config/defaults.toml → [volume]",
        },
      ],
    },
    {
      key: "settings_grp_scoring",
      rows: [
        {
          key: "set_scoring_model",
          value: "sonnet",
          source: "config/user_profile.md",
        },
      ],
    },
  ]);
  renderSettings();

  const html = root.innerHTML;
  assert.ok(html.includes("stg-group"), "group hook present");
  assert.ok(html.includes("stg-row"), "row hook present");
  assert.ok(html.includes(">40<"), "numeric value renders");
  assert.ok(html.includes("sonnet"), "string value renders");
  assert.ok(html.includes("config/defaults.toml"), "source pointer renders");
  assert.ok(html.includes("settings-header-title"), "header renders");
});

test("a null/empty value renders the dash placeholder, not blank", () => {
  setGroups([
    {
      key: "settings_grp_volume",
      rows: [
        {
          key: "set_daily_scoring_limit",
          value: null,
          source: "config/defaults.toml",
        },
      ],
    },
  ]);
  renderSettings();
  assert.ok(root.innerHTML.includes("—"));
});

test("no groups shows the empty state, not a blank sheet", () => {
  setGroups([]);
  renderSettings();
  assert.ok(root.innerHTML.includes("stg-empty"));
});

test("settings values and sources are escaped, not raw", () => {
  setGroups([
    {
      key: "settings_grp_volume",
      rows: [
        {
          key: "set_max_active_companies",
          value: "<script>alert(1)</script>",
          source: 'config <b>"quoted"</b>',
        },
      ],
    },
  ]);
  renderSettings();

  const html = root.innerHTML;
  assert.ok(!html.includes("<script>"), "value script tag is neutralized");
  assert.ok(!html.includes("<b>"), "source markup is neutralized");
  assert.ok(
    html.includes("&quot;quoted&quot;"),
    "quotes in source are escaped",
  );
});

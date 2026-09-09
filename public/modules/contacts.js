// =============================================================================
// contacts.js — the Networking tab: everyone worth writing to, in one list.
//
// The Applications table answers "what have I sent, and what is waiting on
// whom". This is the same question one step earlier: who have I not written to
// yet, who has gone quiet, and what was I going to say to them.
//
// Live-only, like Reports: a networking list is not part of the vacancy
// snapshot, so it is fetched from /api/contacts rather than baked into the
// payload. That also means a status change is a PATCH, not a queued save —
// there is no snapshot to keep in step.
//
// The pure half (grouping, counts, filtering, row markup) is exported for
// `node --test`; only the init/render/handler functions touch the DOM.
// =============================================================================

import { API_BASE } from "./state.js";
import { escHtml, jsAttr, safeUrl, pluralForm } from "./helpers.js";
import { T, dateLocale } from "./i18n.js";

// Twin of statuses.py CONTACT_STATUSES and the SQL CHECK, in funnel order so
// the count strip and the drawer's select both read left-to-right as progress.
export const CONTACT_STATUSES = [
  "planned",
  "contacted",
  "replied",
  "met",
  "declined",
  "stale",
];

// Plain words, never the raw key. English fallbacks; the Russian comes from
// i18n.py through T().
export const CONTACT_STATUS_LABELS = {
  planned: "Planned",
  contacted: "Contacted",
  replied: "Replied",
  met: "Met",
  declined: "Declined",
  stale: "Stale",
};

// One accent per status, from the same vocabulary the Triage board uses for
// its columns: nothing sent yet is neutral, in-flight is warm, an answer is
// green, an ending is grey. Never carried by colour alone — every status also
// shows its word.
export const CONTACT_STATUS_COLORS = {
  planned: "var(--sky-text-tertiary)",
  contacted: "var(--coral)",
  replied: "var(--tier-a)",
  met: "var(--pine)",
  declined: "var(--slate)",
  stale: "var(--sky-text-disabled)",
};

// The channels a contact can be reached on, in the order they are shown:
// EA-native first (where these people actually are), then the general
// networks, then the direct ones.
export const CONTACT_CHANNELS = [
  "ea_forum",
  "linkedin",
  "telegram",
  "x",
  "github",
  "site",
  "email",
  "calendly",
];

// Short, unambiguous words rather than brand icons. The house style bans
// category codes on screen, and a row of unlabelled glyphs is exactly that.
export const CONTACT_CHANNEL_LABELS = {
  ea_forum: "Forum",
  linkedin: "LinkedIn",
  telegram: "Telegram",
  x: "X",
  github: "GitHub",
  site: "Site",
  email: "Email",
  calendly: "Calendly",
};

// ---------------------------------------------------------------------------
// Pure logic
// ---------------------------------------------------------------------------

/**
 * A channel's value as an openable URL, or "" when it is not one.
 *
 * The sweeps store whatever they found: a full URL, a bare @handle, an email
 * with a note in brackets after it. A handle becomes a link where the network
 * has a predictable URL shape, an email becomes mailto:, and anything that
 * cannot be resolved renders as text rather than as a link that 404s.
 */
export function channelUrl(channel, value) {
  const raw = String(value == null ? "" : value).trim();
  if (!raw) return "";

  // "a@b.com (live form target)" — take the address, drop the commentary.
  if (channel === "email") {
    const match = raw.match(/[^\s;,<>()]+@[^\s;,<>()]+/);
    return match ? "mailto:" + match[0] : "";
  }

  // Several sources separated by " ; " — the first one is the primary.
  const first = raw.split(/\s*;\s*/)[0].trim();
  if (/^https?:\/\//i.test(first)) return safeUrl(first);

  const handle = first.replace(/^@/, "");
  if (!handle || /\s/.test(handle)) return "";
  if (channel === "telegram") return safeUrl("https://t.me/" + handle);
  if (channel === "x") return safeUrl("https://x.com/" + handle);
  if (channel === "github") return safeUrl("https://github.com/" + handle);
  if (channel === "linkedin")
    return safeUrl("https://www.linkedin.com/in/" + handle);
  return "";
}

/**
 * A sweep's "not established" value, as something a reader can take at a
 * glance.
 *
 * The sweeps write `? (Ankara / Turkey)` for "we could not confirm the city,
 * and here is what we do know", and a bare `?` for "we looked and found
 * nothing". Rendered raw, that shorthand leaks onto the screen and a leading
 * question mark reads as a broken value rather than as a hedge.
 *
 * The parenthesised remainder already reads as tentative in plain English, so
 * the marker is dropped and the parentheses kept. Nothing is invented: an
 * uncertain city stays uncertain, it just stops shouting about it. The stored
 * value is untouched — the database keeps the sweep's own words.
 */
export function displayValue(value) {
  const text = String(value == null ? "" : value).trim();
  if (!text || text === "?") return "";
  const hedged = text.match(/^\?\s*\((.+)\)$/);
  return hedged ? "(" + hedged[1] + ")" : text;
}

/** The channels a contact actually has, in display order. */
export function contactChannels(contact) {
  const channels = (contact && contact.channels) || {};
  return CONTACT_CHANNELS.filter((c) => channels[c]).map((c) => ({
    key: c,
    value: channels[c],
    url: channelUrl(c, channels[c]),
  }));
}

/** Every group present, most people first, then alphabetically. */
export function groupsOf(contacts) {
  const counts = new Map();
  for (const c of contacts || []) {
    const g = (c && c.group) || "other";
    counts.set(g, (counts.get(g) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([group, count]) => ({ group, count }));
}

/** How many contacts sit in each status — zeroes kept.
 *
 *  Unlike the Applications count strip, this one keeps its zeroes: the list is
 *  a queue, and "0 replied" is the number that says a sweep has not paid off
 *  yet. Hiding it would make an unanswered list look like an untouched one. */
export function countByStatus(contacts) {
  const counts = {};
  for (const s of CONTACT_STATUSES) counts[s] = 0;
  for (const c of contacts || []) {
    const s = c && c.status;
    if (s in counts) counts[s] += 1;
  }
  return counts;
}

/** Contacts in one group, or all of them when the filter is off. */
export function filterByGroup(contacts, group) {
  if (!group) return contacts || [];
  return (contacts || []).filter((c) => (c && c.group) === group);
}

/**
 * The count strip: "27 people · 24 planned · 3 contacted · 0 replied".
 *
 * The total leads because it is the one number that answers "how big is this
 * list"; the statuses follow in funnel order.
 */
export function countStripText(contacts, t) {
  const translate = t || ((k, fb) => fb);
  const total = (contacts || []).length;
  const people = translate(
    "contacts_count_" + pluralForm(total),
    total === 1 ? "person" : "people",
  );
  const counts = countByStatus(contacts);
  const parts = [total + " " + people];
  for (const s of CONTACT_STATUSES) {
    parts.push(
      counts[s] +
        " " +
        translate(
          "contact_status_" + s,
          CONTACT_STATUS_LABELS[s],
        ).toLowerCase(),
    );
  }
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Markup
// ---------------------------------------------------------------------------

function channelsCell(contact) {
  const channels = contactChannels(contact);
  if (!channels.length) return "—";
  return channels
    .map((c) => {
      const label = escHtml(
        T("contact_channel_" + c.key, CONTACT_CHANNEL_LABELS[c.key]),
      );
      if (!c.url) {
        // A handle with no resolvable URL is still information — it says the
        // person is reachable there — so it shows as text rather than
        // vanishing or pretending to be a link.
        return (
          '<span class="contact-channel contact-channel--plain">' +
          label +
          "</span>"
        );
      }
      return (
        '<a class="contact-channel" href="' +
        escHtml(c.url) +
        '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' +
        label +
        "</a>"
      );
    })
    .join("");
}

export function buildContactRow(contact, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const status = contact.status || "planned";
  const statusLabel = translate(
    "contact_status_" + status,
    CONTACT_STATUS_LABELS[status] || status,
  );
  const idAttr = jsAttr(contact.id);

  const roleAtOrg = [contact.role, contact.org].filter(Boolean).join(" @ ");

  // The name as they write it, shown under the Latin form rather than instead
  // of it — a message has to be addressed the way they spell it, and the list
  // has to stay scannable by someone who does not read that script.
  const nameCell =
    '<td class="contact-cell contact-cell-name">' +
    '<span class="contact-name">' +
    escHtml(contact.name || "") +
    "</span>" +
    (contact.name_local
      ? '<span class="contact-name-local">' +
        escHtml(contact.name_local) +
        "</span>"
      : "") +
    "</td>";

  const full = (text) => (text ? ' title="' + escHtml(text) + '"' : "");

  return (
    '<tr class="contact-row" data-id="' +
    escHtml(contact.id) +
    '" role="button" tabindex="0" onclick="openContact(\'' +
    idAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openContact('" +
    idAttr +
    "')}\">" +
    nameCell +
    '<td class="contact-cell contact-cell-city"' +
    full(displayValue(contact.city)) +
    ">" +
    escHtml(displayValue(contact.city) || "—") +
    "</td>" +
    '<td class="contact-cell contact-cell-role"' +
    full(roleAtOrg) +
    ">" +
    escHtml(roleAtOrg || "—") +
    "</td>" +
    '<td class="contact-cell contact-cell-group">' +
    escHtml(contact.group || "") +
    "</td>" +
    '<td class="contact-cell contact-cell-why"' +
    full(contact.why_matters) +
    ">" +
    escHtml(contact.why_matters || "—") +
    "</td>" +
    '<td class="contact-cell contact-cell-channels">' +
    channelsCell(contact) +
    "</td>" +
    '<td class="contact-cell contact-cell-status">' +
    '<span class="contact-status-dot" style="background:' +
    (CONTACT_STATUS_COLORS[status] || "var(--muted)") +
    '"></span>' +
    escHtml(statusLabel) +
    "</td>" +
    '<td class="contact-cell contact-cell-active"' +
    full(displayValue(contact.last_active)) +
    ">" +
    escHtml(displayValue(contact.last_active) || "—") +
    "</td>" +
    "</tr>"
  );
}

export function buildGroupFilter(groups, active, t) {
  const translate = t || ((k, fb) => fb);
  const all =
    '<button type="button" class="contact-filter' +
    (active ? "" : " contact-filter--on") +
    '" onclick="filterContacts(\'\')">' +
    escHtml(translate("contacts_all_groups", "All")) +
    "</button>";
  const rest = groups
    .map(
      (g) =>
        '<button type="button" class="contact-filter' +
        (active === g.group ? " contact-filter--on" : "") +
        '" onclick="filterContacts(\'' +
        jsAttr(g.group) +
        "')\">" +
        escHtml(g.group) +
        '<span class="contact-filter-count">' +
        g.count +
        "</span>" +
        "</button>",
    )
    .join("");
  return '<div class="contact-filters">' + all + rest + "</div>";
}

export function buildContactsTable(contacts, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);

  const headers = [
    ["contacts_col_name", "Name"],
    ["contacts_col_city", "City"],
    ["contacts_col_role", "Role @ Org"],
    ["contacts_col_group", "Group"],
    ["contacts_col_why", "Why"],
    ["contacts_col_channels", "Channels"],
    ["contacts_col_status", "Status"],
    ["contacts_col_active", "Last activity"],
  ]
    .map(
      ([key, fallback]) => "<th>" + escHtml(translate(key, fallback)) + "</th>",
    )
    .join("");

  const rows = contacts.map((c) => buildContactRow(c, options)).join("");

  return (
    '<div class="contacts-table-scroll">' +
    '<table class="contacts-table"><thead><tr>' +
    headers +
    "</tr></thead><tbody>" +
    rows +
    "</tbody></table></div>"
  );
}

/** The drawer: why this person, the opener to send, and where to send it. */
export function buildContactDrawer(contact, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const status = contact.status || "planned";

  const meta = [contact.role, contact.org, contact.city]
    .filter(Boolean)
    .join(" · ");

  const statusOptions = CONTACT_STATUSES.map(
    (s) =>
      '<option value="' +
      s +
      '"' +
      (s === status ? " selected" : "") +
      ">" +
      escHtml(translate("contact_status_" + s, CONTACT_STATUS_LABELS[s])) +
      "</option>",
  ).join("");

  const channels = contactChannels(contact);
  const channelList = channels.length
    ? '<div class="contact-drawer-channels">' + channelsCell(contact) + "</div>"
    : '<p class="contact-drawer-empty">' +
      escHtml(translate("contacts_no_channel", "No channel on file yet.")) +
      "</p>";

  // The opener is the expensive thing to reproduce, so it is the one element
  // with a copy button: the whole point of the drawer is to get that text into
  // the message window without retyping it.
  const opener = contact.opener
    ? '<div class="contact-drawer-block">' +
      '<div class="contact-drawer-label">' +
      escHtml(translate("contacts_opener", "Opener")) +
      '<button type="button" class="contact-copy" onclick="copyContactOpener(\'' +
      jsAttr(contact.id) +
      "')\">" +
      escHtml(translate("contacts_copy", "Copy")) +
      "</button>" +
      "</div>" +
      '<p class="contact-opener-text" id="contactOpener">' +
      escHtml(contact.opener) +
      "</p>" +
      "</div>"
    : "";

  const block = (labelKey, labelFallback, text) =>
    text
      ? '<div class="contact-drawer-block">' +
        '<div class="contact-drawer-label">' +
        escHtml(translate(labelKey, labelFallback)) +
        "</div>" +
        "<p>" +
        escHtml(text) +
        "</p>" +
        "</div>"
      : "";

  return (
    '<div class="contact-drawer" role="dialog" aria-modal="true">' +
    '<button type="button" class="contact-drawer-close" onclick="closeContact()" aria-label="' +
    escHtml(translate("contacts_close", "Close")) +
    '">×</button>' +
    '<h2 class="contact-drawer-name">' +
    escHtml(contact.name || "") +
    (contact.name_local
      ? '<span class="contact-drawer-local">' +
        escHtml(contact.name_local) +
        "</span>"
      : "") +
    "</h2>" +
    (meta
      ? '<div class="contact-drawer-meta">' + escHtml(meta) + "</div>"
      : "") +
    '<div class="contact-drawer-block">' +
    '<div class="contact-drawer-label">' +
    escHtml(translate("contacts_col_status", "Status")) +
    "</div>" +
    '<select class="contact-status-select" onchange="setContactStatus(\'' +
    jsAttr(contact.id) +
    "', this.value)\">" +
    statusOptions +
    "</select>" +
    "</div>" +
    block("contacts_col_why", "Why", contact.why_matters) +
    opener +
    '<div class="contact-drawer-block">' +
    '<div class="contact-drawer-label">' +
    escHtml(translate("contacts_col_channels", "Channels")) +
    "</div>" +
    channelList +
    "</div>" +
    block("contacts_col_active", "Last activity", displayValue(contact.last_active)) +
    block("contacts_notes", "Notes", contact.notes) +
    block("contacts_source", "Source", contact.source_path) +
    "</div>" +
    '<div class="contact-drawer-scrim" onclick="closeContact()"></div>'
  );
}

export function buildContactsView(contacts, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const activeGroup = options.group || "";

  if (!contacts || !contacts.length) {
    return (
      '<div class="contacts-empty">' +
      escHtml(
        translate(
          "contacts_empty",
          "Nobody on the list yet. Import one with `vac contact import <file.csv>`.",
        ),
      ) +
      "</div>"
    );
  }

  const shown = filterByGroup(contacts, activeGroup);
  return (
    '<div class="contacts-count-strip">' +
    escHtml(countStripText(shown, translate)) +
    "</div>" +
    buildGroupFilter(groupsOf(contacts), activeGroup, translate) +
    buildContactsTable(shown, options)
  );
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

let _contacts = null;
let _loadError = "";
let _group = "";
let _openId = null;

function host() {
  return document.getElementById("contactsSection");
}

async function fetchJson(path, options) {
  const base = API_BASE || "";
  const res = await fetch(base + path, {
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    ...(options || {}),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

export async function initContacts() {
  _openId = null;
  _loadError = "";
  render();
  try {
    const data = await fetchJson("/api/contacts");
    _contacts = Array.isArray(data.contacts) ? data.contacts : [];
  } catch (err) {
    // Simple mode has no API, so this is the expected path there. Say what is
    // missing rather than showing an empty list, which reads as "you know
    // nobody".
    console.warn("contacts: could not load the list", err);
    _contacts = null;
    _loadError = T(
      "contacts_unavailable",
      "Networking needs the dashboard server — it is not part of the offline snapshot.",
    );
  }
  render();
}

export function renderContacts() {
  render();
}

function render() {
  const el = host();
  if (!el) return;

  if (_loadError) {
    el.innerHTML =
      '<div class="contacts-empty">' + escHtml(_loadError) + "</div>";
    return;
  }
  if (_contacts === null) {
    el.innerHTML =
      '<div class="contacts-loading">' +
      escHtml(T("contacts_loading", "Loading…")) +
      "</div>";
    return;
  }

  let html = buildContactsView(_contacts, {
    t: T,
    locale: dateLocale(),
    group: _group,
  });
  if (_openId) {
    const open = _contacts.find((c) => c.id === _openId);
    if (open) html += buildContactDrawer(open, { t: T, locale: dateLocale() });
  }
  el.innerHTML = html;
}

export function filterContacts(group) {
  _group = group || "";
  render();
}

export function openContact(id) {
  _openId = id;
  render();
}

export function closeContact() {
  _openId = null;
  render();
}

export async function setContactStatus(id, status) {
  const contact = (_contacts || []).find((c) => c.id === id);
  if (!contact) return;
  const previous = contact.status;
  // Optimistic: the select already shows the new value, so re-rendering from
  // the old one would flick it back and read as a rejected change.
  contact.status = status;
  render();
  try {
    await fetchJson("/api/contacts", {
      method: "PATCH",
      body: JSON.stringify({ id, status }),
    });
  } catch (err) {
    console.warn("contacts: status change failed", err);
    contact.status = previous;
    render();
  }
}

export function copyContactOpener(id) {
  const contact = (_contacts || []).find((c) => c.id === id);
  if (!contact || !contact.opener) return;
  const done = () => {
    const btn = document.querySelector(".contact-copy");
    if (!btn) return;
    const original = btn.textContent;
    btn.textContent = T("contacts_copied", "Copied");
    setTimeout(() => {
      btn.textContent = original;
    }, 1400);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(contact.opener).then(done, () => {});
    return;
  }
  // No clipboard API (an insecure origin, an old browser): select the text so
  // the reader can copy it themselves rather than getting a button that does
  // nothing.
  const node = document.getElementById("contactOpener");
  if (node && window.getSelection) {
    const range = document.createRange();
    range.selectNodeContents(node);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
}

if (typeof window !== "undefined") {
  window.openContact = openContact;
  window.closeContact = closeContact;
  window.filterContacts = filterContacts;
  window.setContactStatus = setContactStatus;
  window.copyContactOpener = copyContactOpener;
}

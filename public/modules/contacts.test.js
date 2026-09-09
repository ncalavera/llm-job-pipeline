// The Networking tab's pure half: turning a stored contact into something a
// reader can act on. The sweeps store whatever they found — a full URL, a bare
// @handle, an email with a note after it — so most of what is tested here is
// the gap between "what the file said" and "what the row must show".

import { test } from "node:test";
import assert from "node:assert/strict";

// contacts.js imports state.js, which reads window.VACANCY_DATA at import
// time, so a minimal browser shell goes up first (mirrors reports.test.js).
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

const {
  CONTACT_STATUSES,
  CONTACT_STATUS_LABELS,
  CONTACT_CHANNELS,
  CONTACT_CHANNEL_LABELS,
  channelUrl,
  displayValue,
  contactChannels,
  groupsOf,
  countByStatus,
  filterByGroup,
  countStripText,
  buildContactRow,
  buildContactsView,
  buildContactDrawer,
  buildGroupFilter,
} = await import("./contacts.js");

function contact(over) {
  return {
    id: "c1",
    name: "Ada Lovelace",
    name_local: "",
    city: "Tbilisi",
    org: "Analytical Engines",
    role: "Operations lead",
    why_matters: "Ran the only programme like this in the region",
    channels: {},
    group: "ea-georgia",
    status: "planned",
    last_active: "2026-08-05 forum post",
    opener: "",
    notes: "",
    source_path: "profiles/sweep.csv",
    ...over,
  };
}

// --- Vocabulary -----------------------------------------------------------

test("every status has a plain-word label", () => {
  for (const s of CONTACT_STATUSES) {
    assert.ok(CONTACT_STATUS_LABELS[s], `no label for ${s}`);
    assert.ok(/^[A-Z]/.test(CONTACT_STATUS_LABELS[s]));
  }
});

test("every channel has a plain-word label, never a bare glyph", () => {
  for (const c of CONTACT_CHANNELS) {
    assert.ok(CONTACT_CHANNEL_LABELS[c], `no label for ${c}`);
    assert.ok(/[A-Za-z]/.test(CONTACT_CHANNEL_LABELS[c]));
  }
});

// --- channelUrl -----------------------------------------------------------

test("a full URL is used as it stands", () => {
  assert.equal(
    channelUrl("linkedin", "https://www.linkedin.com/in/someone/"),
    "https://www.linkedin.com/in/someone/",
  );
});

test("a bare handle becomes the network's URL", () => {
  assert.equal(channelUrl("telegram", "@someone"), "https://t.me/someone");
  assert.equal(channelUrl("x", "someone"), "https://x.com/someone");
  assert.equal(channelUrl("github", "@someone"), "https://github.com/someone");
});

test("an email becomes a mailto, and the commentary after it is dropped", () => {
  // The real sweep stores: "a@b.edu (live form target in the JS bundle)".
  assert.equal(
    channelUrl("email", "a@b.edu (live form target in the JS bundle)"),
    "mailto:a@b.edu",
  );
});

test("the first of several sources wins", () => {
  assert.equal(
    channelUrl("site", "https://one.example/ ; https://two.example/"),
    "https://one.example/",
  );
});

test("a value that is not a handle or a URL yields no link", () => {
  // "not found", "see notes" — prose, not an address. A link built from it
  // would 404, which is worse than showing the text.
  assert.equal(channelUrl("linkedin", "asked, no profile"), "");
  assert.equal(channelUrl("site", "personal blog somewhere"), "");
  assert.equal(channelUrl("email", "no address anywhere"), "");
});

test("channelUrl never returns a dangerous scheme", () => {
  assert.equal(channelUrl("site", "javascript:alert(1)"), "");
  assert.equal(channelUrl("site", "data:text/html,x"), "");
});

test("an empty value yields no link", () => {
  assert.equal(channelUrl("linkedin", ""), "");
  assert.equal(channelUrl("linkedin", null), "");
});

// --- contactChannels ------------------------------------------------------

test("channels come back in display order, not object order", () => {
  const c = contact({
    channels: { email: "a@b.c", ea_forum: "https://forum.example/u/x" },
  });
  assert.deepEqual(
    contactChannels(c).map((x) => x.key),
    ["ea_forum", "email"],
  );
});

test("a contact with no channels yields none", () => {
  assert.deepEqual(contactChannels(contact()), []);
  assert.deepEqual(contactChannels({}), []);
});

// --- Grouping and counting ------------------------------------------------

test("groups are ordered by size, then alphabetically", () => {
  const list = [
    contact({ id: "1", group: "ea-turkey" }),
    contact({ id: "2", group: "ea-georgia" }),
    contact({ id: "3", group: "ea-georgia" }),
    contact({ id: "4", group: "ea-russian" }),
  ];
  assert.deepEqual(groupsOf(list), [
    { group: "ea-georgia", count: 2 },
    { group: "ea-russian", count: 1 },
    { group: "ea-turkey", count: 1 },
  ]);
});

test("counts keep their zeroes", () => {
  // "0 replied" is the number that says a sweep has not paid off yet. Dropping
  // it would make an unanswered list look like an untouched one.
  const counts = countByStatus([contact({ status: "planned" })]);
  assert.equal(counts.planned, 1);
  assert.equal(counts.replied, 0);
  assert.ok("stale" in counts);
});

test("the group filter narrows, and an empty filter keeps everything", () => {
  const list = [
    contact({ id: "1", group: "ea-georgia" }),
    contact({ id: "2", group: "ea-turkey" }),
  ];
  assert.equal(filterByGroup(list, "ea-turkey").length, 1);
  assert.equal(filterByGroup(list, "").length, 2);
  assert.equal(filterByGroup(list, null).length, 2);
});

test("the count strip leads with the total and then the funnel", () => {
  const list = [
    contact({ id: "1" }),
    contact({ id: "2", status: "contacted" }),
  ];
  const text = countStripText(list);
  assert.ok(text.startsWith("2 people"));
  assert.ok(text.includes("1 planned"));
  assert.ok(text.includes("1 contacted"));
  assert.ok(text.includes("0 replied"));
});

test("the count strip says 'person' for one", () => {
  assert.ok(countStripText([contact()]).startsWith("1 person"));
});

test("the count strip takes the translation for the count's plural form", () => {
  const t = (key, fb) =>
    ({
      contacts_count_one: "<one>",
      contacts_count_few: "<few>",
      contacts_count_many: "<many>",
    })[key] || fb;
  const many = Array.from({ length: 5 }, (_, i) => contact({ id: String(i) }));
  assert.ok(countStripText(many, t).startsWith("5 <many>"));
  assert.ok(countStripText(many.slice(0, 2), t).startsWith("2 <few>"));
  assert.ok(countStripText(many.slice(0, 1), t).startsWith("1 <one>"));
});

// --- Markup ---------------------------------------------------------------

test("a row shows the name, and the local spelling under it when there is one", () => {
  const html = buildContactRow(contact({ name_local: "ადა" }));
  assert.ok(html.includes("Ada Lovelace"));
  assert.ok(html.includes("ადა"));
});

test("a row without a local name renders no empty element for it", () => {
  assert.ok(!buildContactRow(contact()).includes("contact-name-local"));
});

test("role and org are joined into one column", () => {
  assert.ok(
    buildContactRow(contact()).includes("Operations lead @ Analytical Engines"),
  );
});

test("a missing field renders as an em dash, never as a blank cell", () => {
  const html = buildContactRow(
    contact({ city: "", why_matters: "", role: "", org: "" }),
  );
  assert.ok(html.includes("—"));
});

test("the status is shown as a word, not only as a colour", () => {
  // WCAG 1.4.1: colour alone may not carry meaning.
  const html = buildContactRow(contact({ status: "replied" }));
  assert.ok(html.includes("Replied"));
  assert.ok(html.includes("contact-status-dot"));
});

test("a channel with a URL renders as a link that opens in a new tab", () => {
  const html = buildContactRow(
    contact({ channels: { linkedin: "https://www.linkedin.com/in/x" } }),
  );
  assert.ok(html.includes('target="_blank"'));
  assert.ok(html.includes('rel="noopener"'));
  assert.ok(html.includes("LinkedIn"));
});

test("a channel that cannot be resolved renders as text, not as a broken link", () => {
  const html = buildContactRow(
    contact({ channels: { linkedin: "asked, no profile" } }),
  );
  assert.ok(html.includes("contact-channel--plain"));
  assert.ok(!html.includes('href="asked'));
});

test("a row escapes everything a sweep could put in it", () => {
  const html = buildContactRow(
    contact({
      name: "<img src=x onerror=alert(1)>",
      why_matters: '"><script>bad()</script>',
    }),
  );
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script"));
});

test("the empty state says how to fill the list", () => {
  const html = buildContactsView([]);
  assert.ok(html.includes("vac contact import"));
});

test("the view renders a count strip, a filter and a table", () => {
  const html = buildContactsView([contact()]);
  assert.ok(html.includes("contacts-count-strip"));
  assert.ok(html.includes("contact-filters"));
  assert.ok(html.includes("<table"));
});

test("the view honours the active group filter", () => {
  const list = [
    contact({ id: "1", name: "In Georgia", group: "ea-georgia" }),
    contact({ id: "2", name: "In Turkey", group: "ea-turkey" }),
  ];
  const html = buildContactsView(list, { group: "ea-turkey" });
  assert.ok(html.includes("In Turkey"));
  assert.ok(!html.includes(">In Georgia<"));
});

test("the filter marks exactly one button as active", () => {
  const groups = [
    { group: "ea-georgia", count: 2 },
    { group: "ea-turkey", count: 1 },
  ];
  const html = buildGroupFilter(groups, "ea-turkey");
  assert.equal(html.match(/contact-filter--on/g).length, 1);
});

test("with no filter chosen, All is the active button", () => {
  const html = buildGroupFilter([{ group: "ea-georgia", count: 1 }], "");
  assert.ok(html.includes('contact-filter contact-filter--on"'));
});

// --- Drawer ---------------------------------------------------------------

test("the drawer offers every status, with the current one selected", () => {
  const html = buildContactDrawer(contact({ status: "met" }));
  for (const s of CONTACT_STATUSES) {
    assert.ok(html.includes('value="' + s + '"'), `no option for ${s}`);
  }
  assert.ok(html.includes('value="met" selected'));
});

test("the drawer shows a copy button only when there is an opener to copy", () => {
  assert.ok(!buildContactDrawer(contact()).includes("contact-copy"));
  assert.ok(
    buildContactDrawer(contact({ opener: "Hello" })).includes("contact-copy"),
  );
});

test("the drawer says so when there is no channel, rather than showing nothing", () => {
  const html = buildContactDrawer(contact());
  assert.ok(html.includes("No channel on file yet"));
});

test("the drawer omits a block whose field is empty", () => {
  const html = buildContactDrawer(contact({ notes: "", source_path: "" }));
  assert.ok(!html.includes("Notes"));
  assert.ok(!html.includes("Source"));
});

test("the drawer escapes the opener", () => {
  const html = buildContactDrawer(
    contact({ opener: "<script>bad()</script>" }),
  );
  assert.ok(!html.includes("<script>bad"));
});

test("the drawer has a scrim that closes it", () => {
  const html = buildContactDrawer(contact());
  assert.ok(html.includes("contact-drawer-scrim"));
  assert.ok(html.includes("closeContact()"));
});


// --- displayValue ---------------------------------------------------------
// The sweeps hedge with "? (what we do know)". That shorthand belongs in the
// file, not on the screen.

test("a hedged value keeps what is known and drops the question mark", () => {
  assert.equal(displayValue("? (Ankara / Turkey)"), "(Ankara / Turkey)");
  assert.equal(displayValue("? (not stated)"), "(not stated)");
});

test("a bare question mark shows nothing at all", () => {
  assert.equal(displayValue("?"), "");
  assert.equal(displayValue("  ?  "), "");
});

test("a plain value is left exactly as it is", () => {
  assert.equal(displayValue("Tbilisi, Georgia"), "Tbilisi, Georgia");
  // A question mark that is part of the text, not a hedge marker, survives.
  assert.equal(displayValue("Who Knows? Ltd"), "Who Knows? Ltd");
});

test("displayValue never throws on nothing", () => {
  assert.equal(displayValue(null), "");
  assert.equal(displayValue(undefined), "");
  assert.equal(displayValue(""), "");
});

test("a hedged city renders without the marker, and an empty one as a dash", () => {
  assert.ok(buildContactRow(contact({ city: "? (Turkey)" })).includes("(Turkey)"));
  const bare = buildContactRow(contact({ city: "?" }));
  assert.ok(bare.includes("—"));
  assert.ok(!bare.includes(">?<"));
});

import { screenLists, screenRequirements } from "./derive.js";

// These are navigation categories, not a judgment of suitability. Unrecognised
// functions remain visible in Other roles; no score or preference excludes them.
const FUNCTIONS = [
  [
    "product",
    "Product",
    "Product management and development",
    /\bproduct (?:manager|management|lead|director|owner)|\b(?:head|director|vp) of product\b|\btechnical product\b|продуктов|продакт/iu,
  ],
  [
    "operations",
    "Operations leadership",
    "Operations and organisational coordination",
    /\boperations\b|\bchief of staff\b|\b(?:coo|ceo)\b|\bchief (?:executive|operating)\b|операцион|операций/iu,
  ],
  [
    "research",
    "Research",
    "Research, analysis and evaluation",
    /\bresearch(?:er)?\b|\bevaluation\b|исследован/iu,
  ],
  [
    "programmes",
    "Programmes",
    "Programme and project delivery",
    /\bprogram(?:me)?s?\b|\bproject (?:manager|management|lead|director)\b|программ|проект/iu,
  ],
  [
    "partnerships",
    "Fundraising & partnerships",
    "Fundraising and external partnerships",
    /\bfundrais(?:ing|er)\b|\bpartnerships?\b|фандрайз|партн[её]р/iu,
  ],
];
const OTHER = ["other", "Other roles", "Function needs a closer look"];

function category(role) {
  for (const value of [role.title, role.screening?.posting_facts?.function]) {
    if (typeof value !== "string") continue;
    const matches = FUNCTIONS.filter(([, , , pattern]) => pattern.test(value));
    if (matches.length === 1) return matches[0];
    // A mixed function should not be presented as a confident single category.
    if (matches.length > 1) return OTHER;
  }
  return OTHER;
}

function comparisons(role) {
  const requirements = screenRequirements(role);
  const values = role.screening?.profile_comparison;
  return (Array.isArray(values) ? values : []).filter(
    (c) => c && Number.isInteger(c.requirement) && requirements[c.requirement],
  );
}

function requiredConflict(role) {
  const requirements = screenRequirements(role);
  return comparisons(role).find(
    (c) =>
      c.finding === "possible_conflict" &&
      requirements[c.requirement].strength === "required",
  );
}

function evidenceOrder(role) {
  if (requiredConflict(role)) return 2;
  return comparisons(role).some((c) => c.finding === "match") ? 0 : 1;
}

/** Plain text only: callers must escape it when rendering HTML. */
export function batchConcern(role, t = (key, fallback) => fallback) {
  const conflict = requiredConflict(role);
  if (conflict) {
    const requirement = screenRequirements(role)[conflict.requirement];
    return typeof requirement.value === "string" && requirement.value.trim()
      ? `${t("screen_check_requirement", "Check requirement")}: ${requirement.value.trim()}`
      : t("screen_check_conflict", "Check a possible requirement conflict");
  }
  if (!comparisons(role).length)
    return t("screen_check_fit", "Profile fit still needs checking");
  return t(
    "screen_check_location",
    "Check location and remaining requirements",
  );
}

/** Nonempty functional groups of ready, undecided roles. Never mutates input. */
export function reviewBatches(roles, getStatus, promptFingerprint) {
  const { toScreen } = screenLists(roles, getStatus, promptFingerprint);
  const groups = new Map();
  const seen = new Set();
  for (const role of roles) {
    if (!role || !toScreen.has(role.id) || seen.has(role.id)) continue;
    seen.add(role.id);
    const [key, label, reason] = category(role);
    if (!groups.has(key)) groups.set(key, { key, label, reason, roles: [] });
    groups.get(key).roles.push(role);
  }
  const order = [...FUNCTIONS, OTHER].map(([key]) => key);
  return [...groups.values()]
    .sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key))
    .map((group) => ({
      ...group,
      roles: group.roles.sort(
        (a, b) =>
          evidenceOrder(a) - evidenceOrder(b) ||
          String(a.title || "").localeCompare(String(b.title || ""), "en") ||
          String(a.id).localeCompare(String(b.id), "en"),
      ),
    }));
}

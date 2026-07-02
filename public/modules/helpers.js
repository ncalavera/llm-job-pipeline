// =============================================================================
// helpers.js — Pure utility functions (no state dependencies)
// Geo data tables are large (~400 lines) but inert lookup data.
// =============================================================================

// ---------------------------------------------------------------------------
// HTML escaping
// ---------------------------------------------------------------------------

export function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Escape a value for use INSIDE a single-quoted JS string that itself sits in a
// double-quoted HTML attribute, e.g. onclick="fn('<value>')". escHtml alone is
// not enough: it leaves ' untouched, so a value like the slug "women's-world-
// banking" or name "Women's World Banking" closes the JS string early and the
// handler throws a SyntaxError (the row/button silently does nothing). Escape
// backslash + apostrophe for JS on top of the HTML escaping.
export function jsAttr(str) {
  return escHtml(str).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------

export function relativeTime(dateStr) {
  if (!dateStr) return "\u2014";
  var d = new Date(dateStr);
  if (isNaN(d.getTime())) return "\u2014";
  var diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 60) return "now";
  var m = Math.floor(diff / 60);
  if (m < 60) return m + "m ago";
  var h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  var days = Math.floor(h / 24);
  if (days < 30) return days + "d ago";
  var mo = Math.floor(days / 30);
  if (mo < 12) return mo + "mo ago";
  return Math.floor(mo / 12) + "y ago";
}

// ---------------------------------------------------------------------------
// Geo data — Country flags, known countries, known regions
// ---------------------------------------------------------------------------

export const COUNTRY_FLAGS = {
  afghanistan: "\u{1F1E6}\u{1F1EB}",
  albania: "\u{1F1E6}\u{1F1F1}",
  algeria: "\u{1F1E9}\u{1F1FF}",
  andorra: "\u{1F1E6}\u{1F1E9}",
  angola: "\u{1F1E6}\u{1F1F4}",
  argentina: "\u{1F1E6}\u{1F1F7}",
  armenia: "\u{1F1E6}\u{1F1F2}",
  australia: "\u{1F1E6}\u{1F1FA}",
  austria: "\u{1F1E6}\u{1F1F9}",
  azerbaijan: "\u{1F1E6}\u{1F1FF}",
  bahamas: "\u{1F1E7}\u{1F1F8}",
  bahrain: "\u{1F1E7}\u{1F1ED}",
  bangladesh: "\u{1F1E7}\u{1F1E9}",
  barbados: "\u{1F1E7}\u{1F1E7}",
  belarus: "\u{1F1E7}\u{1F1FE}",
  belgium: "\u{1F1E7}\u{1F1EA}",
  belize: "\u{1F1E7}\u{1F1FF}",
  benin: "\u{1F1E7}\u{1F1EF}",
  bhutan: "\u{1F1E7}\u{1F1F9}",
  bolivia: "\u{1F1E7}\u{1F1F4}",
  "bosnia and herzegovina": "\u{1F1E7}\u{1F1E6}",
  botswana: "\u{1F1E7}\u{1F1FC}",
  brazil: "\u{1F1E7}\u{1F1F7}",
  brunei: "\u{1F1E7}\u{1F1F3}",
  bulgaria: "\u{1F1E7}\u{1F1EC}",
  "burkina faso": "\u{1F1E7}\u{1F1EB}",
  burundi: "\u{1F1E7}\u{1F1EE}",
  cambodia: "\u{1F1F0}\u{1F1ED}",
  cameroon: "\u{1F1E8}\u{1F1F2}",
  canada: "\u{1F1E8}\u{1F1E6}",
  chad: "\u{1F1F9}\u{1F1E9}",
  chile: "\u{1F1E8}\u{1F1F1}",
  china: "\u{1F1E8}\u{1F1F3}",
  colombia: "\u{1F1E8}\u{1F1F4}",
  comoros: "\u{1F1F0}\u{1F1F2}",
  congo: "\u{1F1E8}\u{1F1EC}",
  "costa rica": "\u{1F1E8}\u{1F1F7}",
  croatia: "\u{1F1ED}\u{1F1F7}",
  cuba: "\u{1F1E8}\u{1F1FA}",
  cyprus: "\u{1F1E8}\u{1F1FE}",
  "czech republic": "\u{1F1E8}\u{1F1FF}",
  czechia: "\u{1F1E8}\u{1F1FF}",
  "democratic republic of the congo": "\u{1F1E8}\u{1F1E9}",
  denmark: "\u{1F1E9}\u{1F1F0}",
  djibouti: "\u{1F1E9}\u{1F1EF}",
  "dominican republic": "\u{1F1E9}\u{1F1F4}",
  ecuador: "\u{1F1EA}\u{1F1E8}",
  egypt: "\u{1F1EA}\u{1F1EC}",
  "el salvador": "\u{1F1F8}\u{1F1FB}",
  "equatorial guinea": "\u{1F1EC}\u{1F1F6}",
  eritrea: "\u{1F1EA}\u{1F1F7}",
  estonia: "\u{1F1EA}\u{1F1EA}",
  eswatini: "\u{1F1F8}\u{1F1FF}",
  ethiopia: "\u{1F1EA}\u{1F1F9}",
  fiji: "\u{1F1EB}\u{1F1EF}",
  finland: "\u{1F1EB}\u{1F1EE}",
  france: "\u{1F1EB}\u{1F1F7}",
  gabon: "\u{1F1EC}\u{1F1E6}",
  gambia: "\u{1F1EC}\u{1F1F2}",
  georgia: "\u{1F1EC}\u{1F1EA}",
  germany: "\u{1F1E9}\u{1F1EA}",
  ghana: "\u{1F1EC}\u{1F1ED}",
  greece: "\u{1F1EC}\u{1F1F7}",
  guatemala: "\u{1F1EC}\u{1F1F9}",
  guinea: "\u{1F1EC}\u{1F1F3}",
  guyana: "\u{1F1EC}\u{1F1FE}",
  haiti: "\u{1F1ED}\u{1F1F9}",
  honduras: "\u{1F1ED}\u{1F1F3}",
  hungary: "\u{1F1ED}\u{1F1FA}",
  iceland: "\u{1F1EE}\u{1F1F8}",
  india: "\u{1F1EE}\u{1F1F3}",
  indonesia: "\u{1F1EE}\u{1F1E9}",
  iran: "\u{1F1EE}\u{1F1F7}",
  iraq: "\u{1F1EE}\u{1F1F6}",
  ireland: "\u{1F1EE}\u{1F1EA}",
  israel: "\u{1F1EE}\u{1F1F1}",
  italy: "\u{1F1EE}\u{1F1F9}",
  "ivory coast": "\u{1F1E8}\u{1F1EE}",
  jamaica: "\u{1F1EF}\u{1F1F2}",
  japan: "\u{1F1EF}\u{1F1F5}",
  jordan: "\u{1F1EF}\u{1F1F4}",
  kazakhstan: "\u{1F1F0}\u{1F1FF}",
  kenya: "\u{1F1F0}\u{1F1EA}",
  kosovo: "\u{1F1FD}\u{1F1F0}",
  kuwait: "\u{1F1F0}\u{1F1FC}",
  kyrgyzstan: "\u{1F1F0}\u{1F1EC}",
  laos: "\u{1F1F1}\u{1F1E6}",
  latvia: "\u{1F1F1}\u{1F1FB}",
  lebanon: "\u{1F1F1}\u{1F1E7}",
  lesotho: "\u{1F1F1}\u{1F1F8}",
  liberia: "\u{1F1F1}\u{1F1F7}",
  libya: "\u{1F1F1}\u{1F1FE}",
  liechtenstein: "\u{1F1F1}\u{1F1EE}",
  lithuania: "\u{1F1F1}\u{1F1F9}",
  luxembourg: "\u{1F1F1}\u{1F1FA}",
  madagascar: "\u{1F1F2}\u{1F1EC}",
  malawi: "\u{1F1F2}\u{1F1FC}",
  malaysia: "\u{1F1F2}\u{1F1FE}",
  maldives: "\u{1F1F2}\u{1F1FB}",
  mali: "\u{1F1F2}\u{1F1F1}",
  malta: "\u{1F1F2}\u{1F1F9}",
  mauritania: "\u{1F1F2}\u{1F1F7}",
  mauritius: "\u{1F1F2}\u{1F1FA}",
  mexico: "\u{1F1F2}\u{1F1FD}",
  moldova: "\u{1F1F2}\u{1F1E9}",
  monaco: "\u{1F1F2}\u{1F1E8}",
  mongolia: "\u{1F1F2}\u{1F1F3}",
  montenegro: "\u{1F1F2}\u{1F1EA}",
  morocco: "\u{1F1F2}\u{1F1E6}",
  mozambique: "\u{1F1F2}\u{1F1FF}",
  myanmar: "\u{1F1F2}\u{1F1F2}",
  namibia: "\u{1F1F3}\u{1F1E6}",
  nepal: "\u{1F1F3}\u{1F1F5}",
  netherlands: "\u{1F1F3}\u{1F1F1}",
  "new zealand": "\u{1F1F3}\u{1F1FF}",
  nicaragua: "\u{1F1F3}\u{1F1EE}",
  niger: "\u{1F1F3}\u{1F1EA}",
  nigeria: "\u{1F1F3}\u{1F1EC}",
  "north korea": "\u{1F1F0}\u{1F1F5}",
  "north macedonia": "\u{1F1F2}\u{1F1F0}",
  norway: "\u{1F1F3}\u{1F1F4}",
  oman: "\u{1F1F4}\u{1F1F2}",
  pakistan: "\u{1F1F5}\u{1F1F0}",
  palestine: "\u{1F1F5}\u{1F1F8}",
  panama: "\u{1F1F5}\u{1F1E6}",
  "papua new guinea": "\u{1F1F5}\u{1F1EC}",
  paraguay: "\u{1F1F5}\u{1F1FE}",
  peru: "\u{1F1F5}\u{1F1EA}",
  philippines: "\u{1F1F5}\u{1F1ED}",
  poland: "\u{1F1F5}\u{1F1F1}",
  portugal: "\u{1F1F5}\u{1F1F9}",
  qatar: "\u{1F1F6}\u{1F1E6}",
  romania: "\u{1F1F7}\u{1F1F4}",
  russia: "\u{1F1F7}\u{1F1FA}",
  rwanda: "\u{1F1F7}\u{1F1FC}",
  "saudi arabia": "\u{1F1F8}\u{1F1E6}",
  senegal: "\u{1F1F8}\u{1F1F3}",
  serbia: "\u{1F1F7}\u{1F1F8}",
  "sierra leone": "\u{1F1F8}\u{1F1F1}",
  singapore: "\u{1F1F8}\u{1F1EC}",
  slovakia: "\u{1F1F8}\u{1F1F0}",
  slovenia: "\u{1F1F8}\u{1F1EE}",
  somalia: "\u{1F1F8}\u{1F1F4}",
  "south africa": "\u{1F1FF}\u{1F1E6}",
  "south korea": "\u{1F1F0}\u{1F1F7}",
  "south sudan": "\u{1F1F8}\u{1F1F8}",
  spain: "\u{1F1EA}\u{1F1F8}",
  "sri lanka": "\u{1F1F1}\u{1F1F0}",
  sudan: "\u{1F1F8}\u{1F1E9}",
  suriname: "\u{1F1F8}\u{1F1F7}",
  sweden: "\u{1F1F8}\u{1F1EA}",
  switzerland: "\u{1F1E8}\u{1F1ED}",
  syria: "\u{1F1F8}\u{1F1FE}",
  taiwan: "\u{1F1F9}\u{1F1FC}",
  tajikistan: "\u{1F1F9}\u{1F1EF}",
  tanzania: "\u{1F1F9}\u{1F1FF}",
  thailand: "\u{1F1F9}\u{1F1ED}",
  togo: "\u{1F1F9}\u{1F1EC}",
  "trinidad and tobago": "\u{1F1F9}\u{1F1F9}",
  tunisia: "\u{1F1F9}\u{1F1F3}",
  turkey: "\u{1F1F9}\u{1F1F7}",
  turkmenistan: "\u{1F1F9}\u{1F1F2}",
  uganda: "\u{1F1FA}\u{1F1EC}",
  ukraine: "\u{1F1FA}\u{1F1E6}",
  "united arab emirates": "\u{1F1E6}\u{1F1EA}",
  "united kingdom": "\u{1F1EC}\u{1F1E7}",
  "united states": "\u{1F1FA}\u{1F1F8}",
  uruguay: "\u{1F1FA}\u{1F1FE}",
  uzbekistan: "\u{1F1FA}\u{1F1FF}",
  venezuela: "\u{1F1FB}\u{1F1EA}",
  vietnam: "\u{1F1FB}\u{1F1F3}",
  yemen: "\u{1F1FE}\u{1F1EA}",
  zambia: "\u{1F1FF}\u{1F1F2}",
  zimbabwe: "\u{1F1FF}\u{1F1FC}",
  uk: "\u{1F1EC}\u{1F1E7}",
  us: "\u{1F1FA}\u{1F1F8}",
  usa: "\u{1F1FA}\u{1F1F8}",
  uae: "\u{1F1E6}\u{1F1EA}",
  drc: "\u{1F1E8}\u{1F1E9}",
};

export const KNOWN_COUNTRIES = new Set([
  "afghanistan",
  "albania",
  "algeria",
  "andorra",
  "angola",
  "argentina",
  "armenia",
  "australia",
  "austria",
  "azerbaijan",
  "bahamas",
  "bahrain",
  "bangladesh",
  "barbados",
  "belarus",
  "belgium",
  "belize",
  "benin",
  "bhutan",
  "bolivia",
  "bosnia and herzegovina",
  "botswana",
  "brazil",
  "brunei",
  "bulgaria",
  "burkina faso",
  "burundi",
  "cambodia",
  "cameroon",
  "canada",
  "chad",
  "chile",
  "china",
  "colombia",
  "comoros",
  "congo",
  "costa rica",
  "croatia",
  "cuba",
  "cyprus",
  "czech republic",
  "czechia",
  "democratic republic of the congo",
  "denmark",
  "djibouti",
  "dominican republic",
  "ecuador",
  "egypt",
  "el salvador",
  "equatorial guinea",
  "eritrea",
  "estonia",
  "eswatini",
  "ethiopia",
  "fiji",
  "finland",
  "france",
  "gabon",
  "gambia",
  "georgia",
  "germany",
  "ghana",
  "greece",
  "guatemala",
  "guinea",
  "guyana",
  "haiti",
  "honduras",
  "hungary",
  "iceland",
  "india",
  "indonesia",
  "iran",
  "iraq",
  "ireland",
  "israel",
  "italy",
  "ivory coast",
  "jamaica",
  "japan",
  "jordan",
  "kazakhstan",
  "kenya",
  "kosovo",
  "kuwait",
  "kyrgyzstan",
  "laos",
  "latvia",
  "lebanon",
  "lesotho",
  "liberia",
  "libya",
  "liechtenstein",
  "lithuania",
  "luxembourg",
  "madagascar",
  "malawi",
  "malaysia",
  "maldives",
  "mali",
  "malta",
  "mauritania",
  "mauritius",
  "mexico",
  "moldova",
  "monaco",
  "mongolia",
  "montenegro",
  "morocco",
  "mozambique",
  "myanmar",
  "namibia",
  "nepal",
  "netherlands",
  "new zealand",
  "nicaragua",
  "niger",
  "nigeria",
  "north korea",
  "north macedonia",
  "norway",
  "oman",
  "pakistan",
  "palestine",
  "panama",
  "papua new guinea",
  "paraguay",
  "peru",
  "philippines",
  "poland",
  "portugal",
  "qatar",
  "romania",
  "russia",
  "rwanda",
  "saudi arabia",
  "senegal",
  "serbia",
  "sierra leone",
  "singapore",
  "slovakia",
  "slovenia",
  "somalia",
  "south africa",
  "south korea",
  "south sudan",
  "spain",
  "sri lanka",
  "sudan",
  "suriname",
  "sweden",
  "switzerland",
  "syria",
  "taiwan",
  "tajikistan",
  "tanzania",
  "thailand",
  "togo",
  "trinidad and tobago",
  "tunisia",
  "turkey",
  "turkmenistan",
  "uganda",
  "ukraine",
  "united arab emirates",
  "united kingdom",
  "united states",
  "uruguay",
  "uzbekistan",
  "venezuela",
  "vietnam",
  "yemen",
  "zambia",
  "zimbabwe",
  "uk",
  "us",
  "usa",
  "uae",
  "drc",
]);

export const KNOWN_REGIONS = new Set([
  "western europe",
  "eastern europe",
  "southern europe",
  "northern europe",
  "central europe",
  "sub-saharan africa",
  "southern africa",
  "northern africa",
  "west africa",
  "east africa",
  "central africa",
  "latin america",
  "latin america and caribbean",
  "central america",
  "south america",
  "north america",
  "southeast asia",
  "south asia",
  "east asia",
  "central asia",
  "middle east",
  "caribbean",
  "pacific",
  "oceania",
  "asia pacific",
  "emea",
  "apac",
  "mena",
  "americas",
]);

// ---------------------------------------------------------------------------
// Location parsing
// ---------------------------------------------------------------------------

// Continent buckets keyed by country / region words. No continent is
// privileged — classification is purely structural. Keep keys lowercase.
const REGION_KEYWORDS = {
  europe: [
    "europe",
    "emea",
    "uk",
    "united kingdom",
    "ireland",
    "germany",
    "france",
    "spain",
    "portugal",
    "italy",
    "netherlands",
    "belgium",
    "switzerland",
    "austria",
    "poland",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "czech",
    "romania",
    "greece",
    "hungary",
    "ukraine",
  ],
  americas: [
    "americas",
    "north america",
    "south america",
    "latin america",
    "usa",
    "united states",
    "canada",
    "mexico",
    "brazil",
    "argentina",
    "chile",
    "colombia",
    "peru",
  ],
  asia: [
    "asia",
    "apac",
    "india",
    "china",
    "japan",
    "singapore",
    "korea",
    "indonesia",
    "philippines",
    "vietnam",
    "thailand",
    "malaysia",
    "middle east",
    "mena",
    "uae",
    "israel",
    "turkey",
  ],
  africa: [
    "africa",
    "nigeria",
    "kenya",
    "south africa",
    "egypt",
    "ghana",
    "ethiopia",
    "morocco",
    "tanzania",
    "uganda",
  ],
};

export function getRegionClass(locText) {
  const l = locText.toLowerCase();
  // Match against each continent's keywords without favouring any region.
  for (const [region, keywords] of Object.entries(REGION_KEYWORDS)) {
    if (keywords.some((k) => l.includes(k))) return region;
  }
  // No country/region signal — distinguish remote-only from unknown.
  if (l.includes("remote")) return "remote";
  return "other";
}

export function parseLocationChips(rawLocation) {
  if (!rawLocation) return [];

  const commaCount = (rawLocation.match(/,/g) || []).length;
  if (commaCount <= 1) {
    return [{ text: rawLocation.trim(), region: getRegionClass(rawLocation) }];
  }

  const tokens = rawLocation
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  const chips = [];
  const seen = new Set();
  let i = 0;

  while (i < tokens.length) {
    const tok = tokens[i];
    const tokLower = tok.toLowerCase();

    if (KNOWN_REGIONS.has(tokLower)) {
      if (!seen.has(tokLower)) {
        seen.add(tokLower);
        chips.push({ text: tok, region: getRegionClass(tok), isRegion: true });
      }
      i++;
    } else if (KNOWN_COUNTRIES.has(tokLower)) {
      if (!seen.has(tokLower)) {
        const asPartOfPair = chips.some(
          (c) =>
            !c.isRegion && c.text.toLowerCase().endsWith(tok.toLowerCase()),
        );
        if (!asPartOfPair) {
          seen.add(tokLower);
          chips.push({ text: tok, region: getRegionClass(tok) });
        }
      }
      i++;
    } else {
      if (
        i + 1 < tokens.length &&
        KNOWN_COUNTRIES.has(tokens[i + 1].toLowerCase())
      ) {
        const pair = tok + ", " + tokens[i + 1];
        const pairKey = pair.toLowerCase();
        if (!seen.has(pairKey)) {
          seen.add(pairKey);
          seen.add(tokens[i + 1].toLowerCase());
          chips.push({ text: pair, region: getRegionClass(pair) });
        }
        i += 2;
      } else {
        const key = tokLower;
        if (!seen.has(key)) {
          seen.add(key);
          chips.push({ text: tok, region: getRegionClass(tok) });
        }
        i++;
      }
    }
  }

  return chips.length
    ? chips
    : [{ text: rawLocation.trim(), region: getRegionClass(rawLocation) }];
}

export function getFlagForChip(chip) {
  if (chip.isRegion) return "";
  const text = chip.text.toLowerCase();
  if (COUNTRY_FLAGS[text]) return COUNTRY_FLAGS[text];
  const comma = text.lastIndexOf(",");
  if (comma !== -1) {
    const country = text.slice(comma + 1).trim();
    if (COUNTRY_FLAGS[country]) return COUNTRY_FLAGS[country];
  }
  const dash = text.indexOf(" - ");
  if (dash !== -1) {
    const part = text.slice(0, dash).trim();
    if (COUNTRY_FLAGS[part]) return COUNTRY_FLAGS[part];
  }
  for (const [country, flag] of Object.entries(COUNTRY_FLAGS)) {
    if (country.length >= 4 && text.includes(country)) return flag;
  }
  return "";
}

export function renderLocationChips(chips, opts) {
  const maxVisible = opts.maxVisible || 3;
  const chipClass = opts.chipClass || "loc-chip";
  const useRegionColor = opts.useRegionColor || false;

  const renderChip = (chip, extraCls) => {
    const cls =
      chipClass +
      (useRegionColor ? " " + chip.region : "") +
      (chip.isRegion ? " loc-region-tag" : "") +
      (extraCls || "");
    const flag = getFlagForChip(chip);
    const text = (flag ? flag + " " : "") + escHtml(chip.text);
    if (chip.url) {
      return (
        '<a href="' +
        escHtml(chip.url) +
        '" target="_blank" rel="noopener" class="' +
        cls +
        '">' +
        text +
        "</a>"
      );
    }
    return '<span class="' + cls + '">' + text + "</span>";
  };

  if (chips.length <= maxVisible + 1) {
    return chips.map((c) => renderChip(c)).join("");
  }

  const visible = chips
    .slice(0, maxVisible)
    .map((c) => renderChip(c))
    .join("");
  const hidden = chips
    .slice(maxVisible)
    .map((c) => renderChip(c, " loc-overflow-chip"))
    .join("");
  const remaining = chips.length - maxVisible;
  return (
    visible +
    '<span class="loc-overflow-wrap">' +
    '<button class="loc-more-btn" onclick="event.stopPropagation();this.parentElement.classList.toggle(\'expanded\')">+' +
    remaining +
    " more</button>" +
    '<span class="loc-overflow-chips">' +
    hidden +
    "</span></span>"
  );
}

// ---------------------------------------------------------------------------
// Score / formatting helpers
// ---------------------------------------------------------------------------

export function llmScoreBadge(score) {
  if (score == null || score < 0) return "";
  const cls =
    score >= 75
      ? "llm-excellent"
      : score >= 55
        ? "llm-good"
        : score >= 35
          ? "llm-partial"
          : score >= 15
            ? "llm-weak"
            : "llm-none";
  return '<span class="llm-score-badge ' + cls + '">' + score + "</span>";
}

export function formatDeadlineHtml(deadline, cssPrefix) {
  if (!deadline) return "";
  const dl = new Date(deadline);
  if (isNaN(dl.getTime())) return "";
  const todayD = new Date(new Date().toISOString().slice(0, 10));
  const isExpired = dl < todayD;
  const diffMs = dl - todayD;
  const diffDays = Math.round(diffMs / 86400000);
  const dateStr = dl.toLocaleDateString("en-US", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  let label = "\u23F0 Deadline: " + dateStr;
  if (isExpired) {
    label += " (expired)";
  } else if (diffDays === 0) {
    label += " (today!)";
  } else if (diffDays === 1) {
    label += " (tomorrow!)";
  } else if (diffDays <= 7) {
    label += " (in " + diffDays + "d)";
  }
  // Urgency tiers: past = expired (red), within a week = soon (amber),
  // further out = active (default).
  const cls = isExpired ? "expired" : diffDays <= 7 ? "soon" : "active";
  return (
    '<span class="' +
    cssPrefix +
    " " +
    cssPrefix +
    "--" +
    cls +
    '">' +
    label +
    "</span>"
  );
}

/**
 * Check if a vacancy's deadline has passed (expired/closed).
 * Returns true if deadline is set and in the past, false otherwise.
 */
export function isVacancyExpired(g) {
  if (!g.deadline) return false;
  const dl = new Date(g.deadline);
  if (isNaN(dl.getTime())) return false;
  const todayD = new Date(new Date().toISOString().slice(0, 10));
  return dl < todayD;
}

// ---------------------------------------------------------------------------
// Source freshness + Triage "no longer actual" classification
// ---------------------------------------------------------------------------

// A role not confirmed by its source for this many days is treated as gone /
// probably closed (mirrors STALE_SOURCE_DAYS in scripts/config.py). Shared by
// the Catalog freshness badge and the Triage "Expired" column.
export const STALE_SOURCE_DAYS = 14;

// Whole days since a role's source last confirmed it. null when unknown/invalid.
export function sourceAgeDays(lastSeen) {
  if (!lastSeen) return null;
  const seen = new Date(lastSeen);
  if (isNaN(seen.getTime())) return null;
  return Math.floor((Date.now() - seen.getTime()) / 86400000);
}

// The source stopped confirming the role for STALE_SOURCE_DAYS+ days.
// Boundary: exactly STALE_SOURCE_DAYS counts as stale.
export function isVacancyStale(g) {
  const age = sourceAgeDays(g && g.last_seen);
  return age != null && age >= STALE_SOURCE_DAYS;
}

// "No longer actual": the deadline has lapsed OR the source went quiet.
export function isVacancyGone(g) {
  return isVacancyExpired(g) || isVacancyStale(g);
}

// Statuses pulled into the shared "Expired" column once the role is no longer
// actual. applied/skipped are terminal decisions and stay in their columns.
export const EXPIRABLE_STATUSES = new Set([
  "liked",
  "to_apply",
  "to_research",
  "to_network",
]);

// Decide which Triage board column a deduped entry (carrying _status) belongs
// to, given the set of real column keys. Returns null when the entry has no
// place on the board:
//   - DB status 'expiring' lives in the Today tab, never on the board;
//   - unseen/passed and any unknown status have no column.
// Gone EXPIRABLE_STATUSES collapse into 'expired'; everything else maps 1:1.
export function triageColumnFor(entry, columnKeys) {
  const status = entry && entry._status;
  if (status === "expiring") return null;
  if (EXPIRABLE_STATUSES.has(status) && isVacancyGone(entry)) return "expired";
  return columnKeys && columnKeys.has(status) ? status : null;
}

export function hardReqHtml(g) {
  // US-eligibility flag: "unclear" means we could not confirm from the listing
  // whether the role is workable from outside the US. Confirmed us_only roles
  // are archived upstream, so only "unclear" ever needs surfacing here.
  const eligChip =
    g.us_eligibility === "unclear"
      ? '<span class="hard-req-tag hard-req-tag--warn" title="Could not confirm this role is workable from outside the US">US?</span>'
      : "";
  const reqs = g.llm_hard_requirements || [];
  if (!reqs.length && !eligChip) return "";
  const tagsHtml = reqs
    .map((label) => `<span class="hard-req-tag">${escHtml(label)}</span>`)
    .join("");
  return `<div class="hard-req-row">${eligChip}${tagsHtml}</div>`;
}

export function ratingDotsHtml(value, max) {
  max = max || 5;
  const filled = Math.round(value || 0);
  let dots = "";
  for (let i = 1; i <= max; i++) {
    dots +=
      '<span class="rating-dot' + (i <= filled ? " filled" : "") + '"></span>';
  }
  return '<span class="rating-dots">' + dots + "</span>";
}

// ---------------------------------------------------------------------------
// Triage dedup helpers
// ---------------------------------------------------------------------------

export function normalizeDedupeText(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

export function getTriageDedupeKey(g) {
  const locs = (g.locations || [])
    .map(function (l) {
      return normalizeDedupeText(l.location);
    })
    .filter(Boolean)
    .sort()
    .join("|");
  return [
    normalizeDedupeText(g.org),
    normalizeDedupeText(g.title),
    locs,
    normalizeDedupeText(g.deadline || ""),
  ].join("::");
}

// ---------------------------------------------------------------------------
// Minimal markdown renderer
// ---------------------------------------------------------------------------

export function inlineFormat(text) {
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__(.+?)__/g, "<strong>$1</strong>");
  text = text.replace(/(?<!\w)\*([^\*]+?)\*(?!\w)/g, "<em>$1</em>");
  text = text.replace(/(?<!\w)_([^_]+?)_(?!\w)/g, "<em>$1</em>");
  text = text.replace(/`([^`]+?)`/g, "<code>$1</code>");
  text = text.replace(
    /\[([^\]]+?)\]\(([^)]+?)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>',
  );
  return text;
}

export function mdToHtml(text) {
  if (!text) return "";
  var html = escHtml(text);
  var lines = html.split("\n");
  var out = [];
  var inList = false;
  var inTable = false;

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];

    if (/^### (.+)$/.test(line)) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      if (inTable) {
        out.push("</tbody></table>");
        inTable = false;
      }
      out.push("<h3>" + RegExp.$1 + "</h3>");
      continue;
    }
    if (/^## (.+)$/.test(line)) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      if (inTable) {
        out.push("</tbody></table>");
        inTable = false;
      }
      out.push("<h2>" + RegExp.$1 + "</h2>");
      continue;
    }
    if (/^# (.+)$/.test(line)) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      if (inTable) {
        out.push("</tbody></table>");
        inTable = false;
      }
      out.push("<h1>" + RegExp.$1 + "</h1>");
      continue;
    }

    if (/^---+$/.test(line.trim())) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      if (inTable) {
        out.push("</tbody></table>");
        inTable = false;
      }
      out.push("<hr>");
      continue;
    }

    if (/^\|(.+)\|$/.test(line.trim())) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      var cells = line
        .trim()
        .slice(1, -1)
        .split("|")
        .map(function (c) {
          return c.trim();
        });
      if (
        cells.every(function (c) {
          return /^[-:]+$/.test(c);
        })
      )
        continue;
      if (!inTable) {
        inTable = true;
        out.push(
          "<table><thead><tr>" +
            cells
              .map(function (c) {
                return "<th>" + inlineFormat(c) + "</th>";
              })
              .join("") +
            "</tr></thead><tbody>",
        );
      } else {
        out.push(
          "<tr>" +
            cells
              .map(function (c) {
                return "<td>" + inlineFormat(c) + "</td>";
              })
              .join("") +
            "</tr>",
        );
      }
      continue;
    }

    if (inTable) {
      out.push("</tbody></table>");
      inTable = false;
    }

    if (/^[\s]*[-*]\s+(.+)$/.test(line)) {
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push("<li>" + inlineFormat(RegExp.$1) + "</li>");
      continue;
    }

    if (inList) {
      out.push("</ul>");
      inList = false;
    }

    if (/^&gt;\s?(.*)$/.test(line)) {
      out.push("<blockquote>" + inlineFormat(RegExp.$1) + "</blockquote>");
      continue;
    }

    if (line.trim() === "") continue;

    out.push("<p>" + inlineFormat(line) + "</p>");
  }

  if (inList) out.push("</ul>");
  if (inTable) out.push("</tbody></table>");
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Toast notification + scroll-to-top button
// ---------------------------------------------------------------------------

let toastEl = null;
let toastTimer = null;

export function initUI() {
  toastEl = document.createElement("div");
  toastEl.className = "toast";
  document.body.appendChild(toastEl);

  const scrollTopBtn = document.createElement("button");
  scrollTopBtn.className = "scroll-top-btn";
  scrollTopBtn.innerHTML = "&#8679;";
  scrollTopBtn.title = "Back to top";
  scrollTopBtn.addEventListener("click", function () {
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  document.body.appendChild(scrollTopBtn);
  window.addEventListener(
    "scroll",
    function () {
      if (window.scrollY > 300) {
        scrollTopBtn.classList.add("visible");
      } else {
        scrollTopBtn.classList.remove("visible");
      }
    },
    { passive: true },
  );
}

export function showToast(status) {
  const messages = {
    liked: "\u2705 Added to favorites",
    passed: "\uD83D\uDC4E Skipped",
  };
  const msg = messages[status];
  if (!msg || !toastEl) return;
  if (toastTimer) clearTimeout(toastTimer);
  toastEl.className = "toast toast-" + status;
  toastEl.textContent = msg;
  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      toastEl.classList.add("visible");
    });
  });
  toastTimer = setTimeout(function () {
    toastEl.classList.remove("visible");
  }, 2000);
}

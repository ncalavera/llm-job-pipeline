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
// URL sanitization — every href built from vacancy/company data must go
// through this. Only http(s)/mailto become live links; anything else
// (javascript:, data:, vbscript:, ...) renders as plain text instead.
// ---------------------------------------------------------------------------

const SAFE_URL_SCHEME = /^(https?|mailto):/i;

// Mirrors the two WHATWG URL-parser steps browsers apply before reading the
// scheme, so a scheme check can't be bypassed with leading control chars or
// a tab/newline spliced into the middle of the scheme (e.g. "java\tscript:").
export function safeUrl(url) {
  if (!url) return "";
  const cleaned = String(url)
    .replace(/^[\x00-\x20]+|[\x00-\x20]+$/g, "")
    .replace(/[\t\n\r]/g, "");
  return SAFE_URL_SCHEME.test(cleaned) ? cleaned : "";
}

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------

// `t` is the i18n T(key, fallback) function, optional so this still runs (in
// the original English) with no i18n wired up \u2014 callers that already import T
// (all 5 current call sites do) pass it through to localize the unit suffix.
export function relativeTime(dateStr, t) {
  if (!dateStr) return "\u2014";
  var tr = t || ((key, fallback) => fallback);
  var d = new Date(dateStr);
  if (isNaN(d.getTime())) return "\u2014";
  var diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 60) return tr("time_just_now", "now");
  var m = Math.floor(diff / 60);
  if (m < 60) return tr("time_min_ago", "{n}m ago").replace("{n}", m);
  var h = Math.floor(m / 60);
  if (h < 24) return tr("time_hour_ago", "{n}h ago").replace("{n}", h);
  var days = Math.floor(h / 24);
  if (days < 30) return tr("time_day_ago", "{n}d ago").replace("{n}", days);
  var mo = Math.floor(days / 30);
  if (mo < 12) return tr("time_month_ago", "{n}mo ago").replace("{n}", mo);
  return tr("time_year_ago", "{n}y ago").replace("{n}", Math.floor(mo / 12));
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
    const href = safeUrl(chip.url);
    if (href) {
      return (
        '<a href="' +
        escHtml(href) +
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

// A screen-only score (cheap two-pass model, never confirmed by the strong
// model) that is high enough to land in the visible match band looks
// byte-identical to a genuine strong-model score — this small badge is what
// keeps the two distinguishable. The backend already decided WHEN it applies
// (group.screen_only_score, see scripts/report/data_prep.py); this just
// renders it. Old rows with no scored_by produce screen_only_score=false, so
// nothing changes for them.
export function screenScoreBadge(g) {
  if (!g || !g.screen_only_score) return "";
  return (
    '<span class="screen-score-badge" title="screen score — not yet confirmed by the main model">' +
    "screen</span>"
  );
}

// `opts.t` is the i18n T(key, fallback) function and `opts.locale` an Intl
// locale ("en-US"/"ru-RU"); both optional so this stays testable with no
// browser/i18n wired up (falls back to the original English copy).
export function formatDeadlineHtml(deadline, cssPrefix, opts) {
  if (!deadline) return "";
  const t = (opts && opts.t) || ((key, fallback) => fallback);
  const locale = (opts && opts.locale) || "en-US";
  // Take the calendar date as written \u2014 the first 10 chars, "YYYY-MM-DD" \u2014
  // rather than handing the full string to Date(), which parses a bare date
  // as UTC midnight but a datetime-with-no-offset as LOCAL midnight. Slicing
  // first makes the parse deterministic regardless of what the source sent.
  const dl = new Date(String(deadline).slice(0, 10));
  if (isNaN(dl.getTime())) return "";
  const todayD = new Date(new Date().toISOString().slice(0, 10));
  const isExpired = dl < todayD;
  const diffMs = dl - todayD;
  const diffDays = Math.round(diffMs / 86400000);
  // timeZone: "UTC" pins the rendered day/month/year to dl's own UTC-midnight
  // calendar date. Without it, toLocaleDateString reformats in the viewer's
  // local zone and can print a different calendar day than diffDays implies
  // (DHA-369 #2).
  const dateStr = dl.toLocaleDateString(locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  let label = "\u23F0 " + t("deadline_prefix", "Deadline:") + " " + dateStr;
  if (isExpired) {
    label += " " + t("deadline_expired", "(expired)");
  } else if (diffDays === 0) {
    label += " " + t("deadline_today", "(today!)");
  } else if (diffDays === 1) {
    label += " " + t("deadline_tomorrow", "(tomorrow!)");
  } else if (diffDays <= 7) {
    label += " " + t("deadline_in_days", "(in {n}d)").replace("{n}", diffDays);
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

// ---------------------------------------------------------------------------
// Sky redesign — quality/tier color helpers (DHA-385, U1)
// One place for score/tier coloring so no hex value is scattered across
// render modules; each returns a CSS class name bound to the token layer in
// style.css (`--q-*` / `--tier-*`).
// ---------------------------------------------------------------------------

// Quality band a numeric score falls into. Boundaries: >=70 good, 50-69
// moderate, <50 weak (also covers null/undefined/NaN as weak).
export function qualityBand(score) {
  if (score >= 70) return "good";
  if (score >= 50) return "moderate";
  return "weak";
}

export function qualityClass(score) {
  return "q-" + qualityBand(score);
}

const TIER_CLASSES = { S: "tier-s", A: "tier-a", B: "tier-b", C: "tier-c" };

export function tierClass(tier) {
  return TIER_CLASSES[tier] || "tier-unknown";
}

// Word for a score, matching the quality bands one level finer (protocol
// section 1): 90+ Exceptional, 80-89 Strong, 70-79 Good, 50-69 Moderate,
// <50 Weak.
export function scoreLabel(score) {
  if (score >= 90) return "Exceptional";
  if (score >= 80) return "Strong";
  if (score >= 70) return "Good";
  if (score >= 50) return "Moderate";
  return "Weak";
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

// When the same role reaches Triage from two boards it dedupes to a single
// card. The survivor must reflect the FRESHEST sighting across every copy —
// otherwise a stale duplicate can wrongly route a still-live role into the
// "Expired" column. Returns the more recent of two last_seen values; either
// may be null / blank / invalid.
export function freshestLastSeen(a, b) {
  const ta = a ? new Date(a).getTime() : NaN;
  const tb = b ? new Date(b).getTime() : NaN;
  if (isNaN(tb)) return a;
  if (isNaN(ta)) return b;
  return ta >= tb ? a : b;
}

// Reduce triage entries (each carrying _status and _review) to one per dedupe
// key. The survivor is the entry with the lowest status priority; ties keep the
// first-inserted. Across all duplicates the survivor inherits any _review and
// the freshest last_seen, so a stale copy can never send a live role to the
// "Expired" column. `statusPri` maps a status string → number (lower = higher).
export function dedupeTriageEntries(entries, statusPri) {
  const pri = (e) => statusPri[e._status] ?? 99;
  const deduped = new Map();
  entries.forEach(function (entry) {
    const key = getTriageDedupeKey(entry);
    const prev = deduped.get(key);
    if (!prev) {
      deduped.set(key, entry);
      return;
    }
    if (pri(entry) < pri(prev)) {
      if (!entry._review && prev._review) entry._review = prev._review;
      entry.last_seen = freshestLastSeen(entry.last_seen, prev.last_seen);
      deduped.set(key, entry);
    } else {
      if (!prev._review && entry._review) prev._review = entry._review;
      prev.last_seen = freshestLastSeen(prev.last_seen, entry.last_seen);
    }
  });
  return deduped;
}

// ---------------------------------------------------------------------------
// Triage funnel (DHA-396, U12) — the board header's "in db → liked → triaged →
// applied" strip must never disagree with the columns it summarizes, so it is
// derived by the SAME dedupeTriageEntries/triageColumnFor reduction the board
// itself uses, not a second parallel count. Pure and DOM-free (pipeline.js
// can't be imported under `node --test` — see the dedupe-lesson doc), so a
// hand-built fixture can exercise every bucket transition directly.
//
// `entries` are plain objects already carrying `_status` (from
// getGroupStatus) and `_approved` (from isGroupCompanyApproved) — the two
// state.js-dependent lookups pipeline.js resolves before calling in. `opts`:
// { statusPri, statusBasket, columnKeys } — the same lookup tables state.js
// exports (STATUS_PRI / STATUS_BASKET / TRIAGE_COLUMNS keys).
// ---------------------------------------------------------------------------

export function computeTriageFunnel(entries, opts) {
  const statusPri = opts.statusPri;
  const statusBasket = opts.statusBasket;
  const columnKeys = opts.columnKeys;

  const baseTotal = entries.length;
  const approved = entries.filter(function (e) {
    return e._approved;
  });

  let rejectedTotal = 0;
  approved.forEach(function (e) {
    if ((statusBasket[e._status] || "unseen") === "passed") rejectedTotal += 1;
  });

  const buckets = {};
  columnKeys.forEach(function (k) {
    buckets[k] = [];
  });
  const deduped = dedupeTriageEntries(approved, statusPri);
  deduped.forEach(function (entry) {
    const col = triageColumnFor(entry, columnKeys);
    if (col && buckets[col] !== undefined) buckets[col].push(entry);
  });

  const at = (key) => (buckets[key] || []).length;
  const metrics = {
    base_total: baseTotal,
    liked_queue: at("liked"),
    triaged_total:
      at("to_apply") +
      at("to_research") +
      at("to_network") +
      at("skipped") +
      at("applied"),
    in_work: at("to_apply") + at("to_research") + at("to_network"),
    applied_total: at("applied"),
    skipped_total: at("skipped"),
    rejected_total: rejectedTotal,
  };

  return { buckets, metrics };
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
  text = text.replace(/\[([^\]]+?)\]\(([^)]+?)\)/g, function (m, label, url) {
    // `text` was already escHtml'd by mdToHtml before inlineFormat runs, so
    // `url` here is HTML-escaped, not raw — safeUrl still works (the scheme
    // itself has no HTML-special chars) but it must not be escHtml'd again.
    const href = safeUrl(url);
    return href
      ? '<a href="' +
          href +
          '" target="_blank" rel="noopener">' +
          label +
          "</a>"
      : label;
  });
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

// Every status a UI action can set \u2192 its toast {key, fallback}. R18: no status
// the UI can set may fall through showToast silently. Before U6, `to_apply`
// (set from Today's "Send" action and the vacancy page's "Move to apply") had
// no entry and produced no feedback; the vacancy page can set liked/passed/
// to_apply, and the board/Today can set the rest \u2014 all covered here. `key` is
// an i18n key; `fallback` is the original English string.
export const TOAST_MESSAGES = {
  liked: { key: "toast_liked", fallback: "\u2705 Added to favorites" },
  passed: { key: "toast_passed", fallback: "\uD83D\uDC4E Skipped" },
  skipped: { key: "toast_skipped", fallback: "\uD83D\uDC4E Skipped" },
  to_apply: { key: "toast_to_apply", fallback: "\uD83D\uDCE5 Moved to apply" },
  to_research: {
    key: "toast_to_research",
    fallback: "\uD83D\uDD0D Marked for research",
  },
  to_network: {
    key: "toast_to_network",
    fallback: "\uD83E\uDD1D Marked for networking",
  },
  applied: { key: "toast_applied", fallback: "\u2705 Marked as applied" },
};

// Resolve a status to its toast copy, or null when the status is not one a UI
// action sets (e.g. `unseen`, `expiring` \u2014 no "you did X" moment).
export function toastMessage(status) {
  return TOAST_MESSAGES[status] || null;
}

// `t` is the i18n T(key, fallback) resolver, optional so the toast still shows
// (in English) when called without it.
export function showToast(status, t) {
  const m = toastMessage(status);
  if (!m || !toastEl) return;
  const translate = t || ((key, fallback) => fallback);
  const msg = translate(m.key, m.fallback);
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

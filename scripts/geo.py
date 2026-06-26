"""Geography buckets — pure classification of a vacancy location entry.

A location entry is a v2 dict {work_mode, region, country, city, ...} or a
v1 dict with a free-text 'location' key. `geo_bucket()` maps it to one of:

    uk | germany | europe | us | other | unknown

These are neutral structural buckets — none is treated as a "home" region.
Geography preference comes entirely from the user profile's exclude_countries
(see filter_vacancies.py), so the classifier privileges no region by default.

This is the authoritative geo classifier used by the pre-score filter and the
`vac` CLI. The stored `region` field (set at fetch time by classify_region())
is display-only legacy — `geo_bucket` is computed on the fly and does not read
or trust it as primary signal. Country wins, then city, then region/work_mode.
"""

# Structural country/city → bucket DATA, loaded from config/defaults.toml
# ([geo.countries] / [geo.cities]). The classification LOGIC lives here; only
# the data lives in TOML. Buckets are neutral structural labels — no "home".
import re

import settings

_COUNTRY_MAP = settings.geo_country_map()
_CITY_MAP = settings.geo_city_map()

# Compiled word-boundary matchers for every bucket, so a term only matches when
# it stands as its own token. Without this, "us" would match inside "belarus"
# (the city "Minsk, Belarus" → mislabelled US). `\b` treats letters/digits as
# word chars, so "u.s." still matches the "u" and "s" tokens correctly and
# "new york" matches as a phrase. Rebuilt by _rebuild_matchers() on config
# reload (tests swap defaults.toml).
_COUNTRY_RE: dict[str, "re.Pattern | None"] = {}
_CITY_RE: dict[str, "re.Pattern | None"] = {}


def _compile_terms(terms: set) -> "re.Pattern | None":
    """Compile a set of location terms into one word-boundary alternation.

    Returns None when there are no terms (an empty pattern would match every
    string). Each term is escaped and wrapped in \\b…\\b so it only matches on
    token boundaries, never as a substring inside another word.
    """
    cleaned = [t.strip() for t in terms if t and t.strip()]
    if not cleaned:
        return None
    # Longest-first so multi-word phrases win over their fragments.
    cleaned.sort(key=len, reverse=True)
    alt = "|".join(re.escape(t) for t in cleaned)
    return re.compile(r"\b(?:" + alt + r")\b")


#: alias (lowercased) → canonical country (lowercased). Rebuilt on reload.
_COUNTRY_ALIASES: dict[str, str] = {}


def _rebuild_matchers() -> None:
    """Recompile the bucket matchers from the current country/city maps."""
    global _COUNTRY_MAP, _CITY_MAP, _COUNTRY_ALIASES
    _COUNTRY_MAP = settings.geo_country_map()
    _CITY_MAP = settings.geo_city_map()
    _COUNTRY_ALIASES = settings.geo_country_aliases()
    for key in ("uk", "de", "europe", "us", "other"):
        _COUNTRY_RE[key] = _compile_terms(_COUNTRY_MAP.get(key, set()))
        _CITY_RE[key] = _compile_terms(_CITY_MAP.get(key, set()))


def canonical_country(name: str) -> str:
    """Normalize a country name to its canonical form (lowercased).

    Collapses known synonyms of ONE country (usa/us/u.s. → "united states") so
    exact-country exclusion is alias-proof, while keeping DISTINCT countries
    distinct. Unknown names pass through normalized (lowercased + stripped).
    """
    norm = (name or "").lower().strip()
    return _COUNTRY_ALIASES.get(norm, norm)

# Internal bucket ORDER for classification (most specific → least). The TOML key
# is the bucket id; the returned label maps the "de" TOML key to "germany". This
# is a neutral lookup table — no bucket is privileged, the order only resolves
# overlaps (a country listed under several keys takes the earliest).
_BUCKET_ORDER = ("uk", "de", "europe", "us", "other")
_BUCKET_LABEL = {"de": "germany"}  # TOML key → public bucket label

# Legacy stored `region` field → bucket id. Data-driven so no code branches on a
# specific region name; extend the table, not the logic.
_REGION_FIELD_TO_BUCKET = {
    "europe": "europe",
    "us": "us",
    "americas": "us",
}

# Build the word-boundary matchers from the freshly loaded maps (also called by
# tests after they swap defaults.toml + settings.clear_cache()).
_rebuild_matchers()


def _label(bucket_key: str) -> str:
    """Map an internal bucket key to its public label (de → germany)."""
    return _BUCKET_LABEL.get(bucket_key, bucket_key)


def _match(text: str, pattern: "re.Pattern | None") -> bool:
    """True if the compiled bucket pattern matches a whole token in text.

    Token-boundary match (not raw substring) so e.g. the "us" bucket term does
    not fire on the "us" inside "Belarus".
    """
    return bool(pattern and pattern.search(text))


def _bucket_from_text(text: str) -> str | None:
    """Classify a free-text location string by walking the bucket table in
    order. Country names and cities for a bucket are checked together."""
    if not text:
        return None
    for key in _BUCKET_ORDER:
        if _match(text, _COUNTRY_RE.get(key)) or _match(text, _CITY_RE.get(key)):
            return _label(key)
    return None


def bucket_for_country(name: str) -> str:
    """Map a bare country (or city) name to a geo bucket.

    Used to translate the user's profile ``exclude_countries`` entries (free
    text like "united states" / "canada" / "germany") into the same buckets the
    location filter computes. Returns 'unknown' when the name is not recognised.
    """
    text = (name or "").lower().strip()
    if not text:
        return "unknown"
    # Exact country-set match first (handles short tokens cleanly), then the
    # substring matcher (handles cities + loose phrasing). Both walk the table.
    for key in _BUCKET_ORDER:
        if text in _COUNTRY_MAP[key]:
            return _label(key)
    return _bucket_from_text(text) or "unknown"


def _all_country_names() -> set[str]:
    """Every recognised country token across all buckets (lowercased)."""
    out: set[str] = set()
    for terms in _COUNTRY_MAP.values():
        out |= {t.lower().strip() for t in terms if t}
    return out


def _city_to_country() -> dict[str, str]:
    """city (lowercased) → country name (lowercased), from geo.city_country."""
    return {str(k).lower().strip(): str(v).lower().strip()
            for k, v in settings.geo_city_country().items()}


def country_for_location(loc: dict) -> str | None:
    """Return the recognised COUNTRY of a single location entry (lowercased),
    or None when no country can be resolved.

    This is the basis for EXACT-country exclusion (unlike geo_bucket, which
    coarsens many countries into one bucket). Resolution order:
        1. explicit `country` field;
        2. `city` field → country via the city_country map;
        3. a recognised country name found as a whole token in the `city` or
           free-text `location` string.
    A None result means "no resolvable country" → never geo-excluded.
    """
    if not loc:
        return None

    known = _all_country_names()

    # 1) Explicit country field.
    country = (loc.get("country") or "").lower().strip()
    if country:
        # Return the explicit country canonicalized so an exclude list naming it
        # (even via an alias or an unrecognised country) still matches exactly.
        return canonical_country(country)

    city_country = _city_to_country()

    # 2) City field → country (exact city key, then token scan).
    city = (loc.get("city") or "").lower().strip()
    if city:
        if city in city_country:
            return canonical_country(city_country[city])
        hit = _country_in_text(city, known, city_country)
        if hit:
            return canonical_country(hit)

    # 3) Free-text location string.
    loc_text = (loc.get("location") or "").lower().strip()
    if loc_text:
        hit = _country_in_text(loc_text, known, city_country)
        if hit:
            return canonical_country(hit)

    return None


def _country_in_text(text: str, known: set[str], city_country: dict[str, str]) -> str | None:
    """Find a recognised country in free text by whole-token match.

    Checks explicit country names first, then known cities (mapped to their
    country). Token boundaries prevent "us" matching inside "Belarus".
    """
    # Country names (longest first so "united states" beats "us").
    for name in sorted(known, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return name
    # City names → country.
    for city, ctry in sorted(city_country.items(), key=lambda kv: len(kv[0]), reverse=True):
        if re.search(r"\b" + re.escape(city) + r"\b", text):
            return ctry
    return None


def _country_region_map() -> dict[str, str]:
    """country (lowercased canonical) → region id, from geo.country_region."""
    return settings.geo_country_region()


def region_for_country(name: str) -> str | None:
    """Map a bare country name to its world region id, or None if unknown.

    Canonicalizes aliases first (so "usa" → "united states" → "north_america").
    Returns None for an empty/unrecognised name so callers can treat it as
    "no region resolved" (never banned, never penalised on geography).
    """
    text = canonical_country(name)
    if not text:
        return None
    return _country_region_map().get(text)


def is_remote_mode(work_mode: str) -> bool:
    """True if a work_mode string signals remote work (substring match).

    Mirrors the work_mode half of filter_vacancies._entry_is_remote so the
    pre-score filter, the post-score ban, and the soft penalty all agree on what
    "remote" means (e.g. "remote", "fully remote", "remote-friendly")."""
    return "remote" in (work_mode or "").lower()


def country_banned(
    country: str,
    *,
    banned_regions: "frozenset[str] | set[str]",
    keep_countries: "frozenset[str] | set[str]",
    banned_countries: "frozenset[str] | set[str]",
) -> bool:
    """Single source of truth for the geo ban decision on a resolved country.

    Whitelist (keep_countries) always wins; otherwise a country is banned when
    explicitly listed (banned_countries) or sitting in a banned world region.
    Callers pass already-canonicalised sets (see config.GEO_* caches). Remote
    handling is the caller's responsibility — a remote role is reachable
    regardless of country."""
    c = canonical_country(country or "")
    if not c or c in keep_countries:
        return False
    if c in banned_countries:
        return True
    region = region_for_country(c)
    return bool(region and region in banned_regions)


def region_for_location(loc: dict) -> str | None:
    """Return the world region id of a single location entry, or None.

    Resolves the entry's country via country_for_location(), then maps it to a
    region. None means no resolvable country/region → geography stays neutral.
    """
    country = country_for_location(loc)
    if not country:
        return None
    return region_for_country(country)


def geo_bucket(loc: dict) -> str:
    """Map one location entry to a geography bucket.

    Returns: 'uk' | 'germany' | 'europe' | 'us' | 'other' | 'unknown'.

    Decision order: country → city → free-text 'location' → region/work_mode
    fallbacks. 'unknown' means no recognizable location (incl. global remote
    with no country — which the filter treats as KEEP). The mapping is purely
    table-driven; no bucket is special-cased in control flow.
    """
    if not loc:
        return "unknown"

    country = (loc.get("country") or "").lower().strip()
    city = (loc.get("city") or "").lower().strip()
    region = (loc.get("region") or "").lower().strip()
    loc_text = (loc.get("location") or "").lower().strip()

    # 1) Country wins.
    if country:
        for key in _BUCKET_ORDER:
            if country in _COUNTRY_MAP[key]:
                return _label(key)
        # Unrecognized but explicit country → other (recognized-as-a-place).
        return "other"

    # 2) City.
    if city:
        b = _bucket_from_text(city)
        if b:
            return b

    # 3) Free-text 'location' (v1 entries). Multi-city: any non-other match wins.
    if loc_text:
        b = _bucket_from_text(loc_text)
        if b:
            return b

    # 4) Region fallback (legacy stored value — weakest signal, table lookup).
    if region:
        bucket = _REGION_FIELD_TO_BUCKET.get(region)
        if bucket:
            return _label(bucket)

    # 5) Global remote with no place → unknown (filter keeps it).
    return "unknown"

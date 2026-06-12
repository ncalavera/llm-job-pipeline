"""Geography buckets — pure classification of a vacancy location entry.

A location entry is a v2 dict {work_mode, region, country, city, ...} or a
v1 dict with a free-text 'location' key. `geo_bucket()` maps it to one of:

    uk | germany | europe | us | cis | other | unknown

This is the authoritative geo classifier used by the pre-score filter and the
`vac` CLI. The stored `region` field (set at fetch time by classify_region())
is display-only legacy — `geo_bucket` is computed on the fly and does not read
or trust it as primary signal. Country wins, then city, then region/work_mode.
"""

# Country name → bucket (lowercased substring match against country/text)
_UK_COUNTRIES = {"united kingdom", "uk", "great britain", "england", "scotland", "wales"}
_DE_COUNTRIES = {"germany", "deutschland"}
_EUROPE_COUNTRIES = {
    "ireland", "france", "netherlands", "belgium", "luxembourg",
    "spain", "portugal", "italy", "austria", "switzerland",
    "poland", "czech republic", "czechia", "slovakia", "hungary",
    "denmark", "sweden", "norway", "finland", "iceland",
    "estonia", "latvia", "lithuania", "greece", "croatia",
    "slovenia", "romania", "bulgaria", "cyprus", "malta",
    "liechtenstein", "monaco",
}
_US_COUNTRIES = {"united states", "usa", "us", "u.s.", "u.s.a.", "canada"}
_CIS_COUNTRIES = {
    "russia", "russian federation", "belarus", "georgia", "armenia",
    "azerbaijan", "kazakhstan", "uzbekistan", "kyrgyzstan", "tajikistan",
    "turkmenistan", "moldova",
}
# Recognized-but-not-target countries → 'other'
_OTHER_COUNTRIES = {
    "turkey", "israel", "uae", "united arab emirates", "saudi arabia",
    "egypt", "jordan", "iraq", "lebanon", "qatar", "kuwait", "bahrain",
    "india", "china", "japan", "singapore", "south korea", "korea",
    "thailand", "vietnam", "indonesia", "malaysia", "philippines",
    "pakistan", "bangladesh", "hong kong", "taiwan",
    "kenya", "south africa", "nigeria", "ghana", "ethiopia", "tanzania",
    "uganda", "morocco", "tunisia", "senegal", "rwanda", "zambia",
    "australia", "new zealand",
    "brazil", "mexico", "argentina", "chile", "colombia", "peru",
}

# City → bucket
_UK_CITIES = {"london", "oxford", "cambridge", "edinburgh", "manchester",
              "bristol", "glasgow", "birmingham", "leeds", "cardiff", "belfast"}
_DE_CITIES = {"berlin", "munich", "münchen", "frankfurt", "hamburg",
              "cologne", "köln", "stuttgart", "düsseldorf", "leipzig"}
_EUROPE_CITIES = {
    "paris", "amsterdam", "lisbon", "dublin", "geneva", "zurich", "zürich",
    "brussels", "stockholm", "oslo", "vienna", "wien", "madrid", "barcelona",
    "rome", "milan", "warsaw", "prague", "copenhagen", "helsinki",
    "tallinn", "riga", "vilnius", "athens", "budapest", "lyon", "porto",
    "rotterdam", "the hague", "luxembourg", "basel", "bern", "antwerp",
}
_US_CITIES = {
    "new york", "nyc", "san francisco", "washington", "seattle", "boston",
    "chicago", "austin", "los angeles", "redwood city", "berkeley",
    "houston", "denver", "atlanta", "miami", "minneapolis", "reston",
    "bay area", "palo alto", "mountain view", "toronto", "vancouver",
}
_CIS_CITIES = {
    "tbilisi", "yerevan", "baku", "almaty", "tashkent", "minsk", "moscow",
    "saint petersburg", "st petersburg", "nur-sultan", "astana", "bishkek",
    "dushanbe", "ashgabat", "chisinau", "batumi",
}
_OTHER_CITIES = {
    "istanbul", "ankara", "tel aviv", "dubai", "abu dhabi", "cairo",
    "amman", "baghdad", "beirut", "riyadh", "doha",
    "mumbai", "delhi", "bangalore", "bengaluru", "hyderabad", "chennai",
    "beijing", "shanghai", "tokyo", "seoul", "bangkok", "jakarta",
    "manila", "kuala lumpur", "karachi", "dhaka", "hong kong", "taipei",
    "nairobi", "cape town", "johannesburg", "lagos", "accra", "addis ababa",
    "kampala", "casablanca", "dakar", "kigali",
    "sydney", "melbourne",
    "são paulo", "sao paulo", "rio de janeiro", "mexico city",
    "buenos aires", "santiago", "bogota", "bogotá", "lima",
}


def _match(text: str, terms: set) -> bool:
    """True if any term appears as a token-ish substring in text."""
    return any(term in text for term in terms)


def _bucket_from_text(text: str) -> str | None:
    """Classify a free-text location string. Order: UK→DE→EU→CIS→US→other."""
    if not text:
        return None
    # Country names first (more specific), then cities.
    if _match(text, _UK_COUNTRIES) or _match(text, _UK_CITIES):
        return "uk"
    if _match(text, _DE_COUNTRIES) or _match(text, _DE_CITIES):
        return "germany"
    if _match(text, _EUROPE_COUNTRIES) or _match(text, _EUROPE_CITIES):
        return "europe"
    if _match(text, _CIS_COUNTRIES) or _match(text, _CIS_CITIES):
        return "cis"
    if _match(text, _US_COUNTRIES) or _match(text, _US_CITIES):
        return "us"
    if _match(text, _OTHER_COUNTRIES) or _match(text, _OTHER_CITIES):
        return "other"
    return None


def geo_bucket(loc: dict) -> str:
    """Map one location entry to a geography bucket.

    Returns: 'uk' | 'germany' | 'europe' | 'us' | 'cis' | 'other' | 'unknown'.

    Decision order: country → city → free-text 'location' → region/work_mode
    fallbacks. 'unknown' means no recognizable location (incl. global remote
    with no country — which the filter treats as KEEP).
    """
    if not loc:
        return "unknown"

    country = (loc.get("country") or "").lower().strip()
    city = (loc.get("city") or "").lower().strip()
    region = (loc.get("region") or "").lower().strip()
    work_mode = (loc.get("work_mode") or "").lower().strip()
    loc_text = (loc.get("location") or "").lower().strip()

    # 1) Country wins.
    if country:
        if country in _UK_COUNTRIES:
            return "uk"
        if country in _DE_COUNTRIES:
            return "germany"
        if country in _EUROPE_COUNTRIES:
            return "europe"
        if country in _US_COUNTRIES:
            return "us"
        if country in _CIS_COUNTRIES:
            return "cis"
        if country in _OTHER_COUNTRIES:
            return "other"
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

    # 4) Region fallback (legacy stored value — weakest signal).
    if region:
        if region == "europe":
            return "europe"
        if region in ("us", "americas"):
            return "us"

    # 5) Global remote with no place → unknown (filter keeps it).
    return "unknown"

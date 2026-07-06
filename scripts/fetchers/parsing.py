"""Markdown / JSON job parsing and title filters.

Everything that turns scraped content into job dicts and decides what looks
like a real vacancy: the markdown parser, the Firecrawl-JSON normalizer, the
blacklist / junk-title filters and org-name extraction for boards.
"""

import hashlib
import re
import urllib.parse
from collections import Counter

import settings

GENERIC_PIPELINE_TITLE_PATTERNS = [
    r"\bexpression of interest\b",
    r"\btalent pool\b",
    r"\bgeneral application(?:s)?\b",
]

# Location-EXTRACTION hint (never a filter): the city tokens the markdown
# parser looks for near a job link to pull a location substring out of free
# text. The list lives in config/defaults.toml ([parsing] location_hint_cities)
# so it is not an owner-geography filter; broaden it for your own regions.
_LOCATION_HINT_CITIES = settings.parsing_location_hint_cities()
_LOCATION_HINT_RE = (
    (r"\b((?:" + "|".join(re.escape(c) for c in _LOCATION_HINT_CITIES) + r")[^|\n\[\]()]{0,30})")
    if _LOCATION_HINT_CITIES
    else r"(?!x)x"
)  # never-matching regex when empty


_FEATURED_RE = re.compile(r"\s{2,}Featured\s*$")

# Paths that are never job listings (site info pages)
_NON_JOB_PATH_SEGMENTS = re.compile(
    r"/(?:about|team|contact|privacy|terms|faq|donate|press|media|board)(?:/|$)",
    re.IGNORECASE,
)
# Fragment anchors that indicate site sections, not jobs
_NON_JOB_FRAGMENTS = re.compile(
    r"#(?:partners|advisory-board|board|team|staff|leadership|about|contact|donate|footer|header|nav|menu|main)",
    re.IGNORECASE,
)


def _is_non_job_url(url: str) -> bool:
    """Return True if the URL points to a non-job page (about, team, etc.).

    Catches patterns like:
      - example.org/about/#partners
      - example.org/about/
      - example.org/team/#leadership
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    fragment = parsed.fragment

    if _NON_JOB_PATH_SEGMENTS.search(path):
        return True
    if fragment and _NON_JOB_FRAGMENTS.search(f"#{fragment}"):
        return True
    return False


# Source pages whose blocks are staff bios far more often than job postings
# (BUG-8). A block extracted from one of these deserves stricter scrutiny than
# a /careers or /jobs page.
_PEOPLE_PAGE_SEGMENTS = re.compile(
    r"/(?:about|team|people|leadership|staff|our-people|who-we-are|meet-the-team|management)(?:/|$)",
    re.IGNORECASE,
)
# A careers-like segment ANYWHERE in the path wins over people segments:
# /about/careers and /who-we-are/careers are careers pages nested under an
# About section, not people pages.
_CAREERS_PAGE_SEGMENTS = re.compile(
    r"/(?:careers?|jobs?|vacancies|vacancy|positions?|opportunities|join(?:-us)?"
    r"|work-with-us|work-for-us)(?:/|$)",
    re.IGNORECASE,
)


def _is_people_page_url(url: str) -> bool:
    """Return True if the scraped SOURCE page is an about/team/people/leadership
    page — a block extracted from one is a probable staff bio, not a vacancy.

    A careers/jobs segment anywhere in the path overrides: /about/careers is a
    careers page, not a people page.
    """
    if not url:
        return False
    path = urllib.parse.urlparse(url).path
    if _CAREERS_PAGE_SEGMENTS.search(path):
        return False
    return bool(_PEOPLE_PAGE_SEGMENTS.search(path))


def _sanitize_board_title(title: str) -> str:
    """Clean board-specific artifacts from job titles.

    Idealist: "Program Officer** **Episcopal Relief & Development**\\" →
              "Program Officer"
    Fast Forward: "VP of Programs  Featured" → "VP of Programs"
    """
    # Idealist: split on ** **  (bold org name appended to title)
    if "** **" in title:
        title = title.split("** **")[0]
    # Strip leftover markdown bold markers
    title = title.replace("**", "").strip()
    # Fast Forward: remove "Featured" suffix (preceded by 2+ spaces)
    title = _FEATURED_RE.sub("", title)
    return title.strip()


_DEPT_COUNT_SUFFIX_RE = re.compile(r"\s*\(\d{1,2}\)\s*$")


def _has_dept_count_suffix(title: str) -> bool:
    """Match trailing (N) where N has 1-2 digits — department-heading marker.

    Catches non-vacancy entries like "Senior Director's Office, CFO (1)"
    that aggregate departments rather than represent a real role.
    Distinguishes count from year-in-parens like (2026) which is 4 digits.
    """
    return bool(_DEPT_COUNT_SUFFIX_RE.search(title))


_CAREERS_BOILERPLATE_PATTERNS = ("freedom, justice, equality let's get to work",)


def _is_careers_boilerplate(text: str) -> bool:
    """Detect known careers-homepage boilerplate strings (a homepage slogan
    scraped in place of a real listing)."""
    if not text:
        return False
    lower = text.lower()
    return any(p in lower for p in _CAREERS_BOILERPLATE_PATTERNS)


def parse_markdown_jobs(markdown: str, org_name: str, *, url_filter: str = "") -> list[dict]:
    """Extract job listings from scraped markdown content.

    Args:
        url_filter: optional regex pattern — only URLs matching this pattern are accepted.
                    Used to prevent capturing external links (e.g. GovAI → governance.ai/post/).
    """
    jobs = []
    seen_titles = set()
    url_filter_re = re.compile(url_filter) if url_filter else None

    link_pattern = re.compile(r"\[([^\]]{5,100})\]\((https?://[^\)]+)\)", re.IGNORECASE)
    image_ext_re = re.compile(r"\.(webp|jpg|jpeg|png|gif|svg)(\?|$)", re.IGNORECASE)

    # Navigation guard: header/footer menu links repeat on every page section
    # of a scrape (nav links can appear 6+ times), while a job link appears
    # once or twice. A URL repeated 3+ times is site chrome, not a job.
    url_counts = Counter(m.group(2).strip() for m in link_pattern.finditer(markdown))

    for match in link_pattern.finditer(markdown):
        title = match.group(1).strip()
        url = match.group(2).strip()
        # Skip image links: ![alt](img-url) captured as [!alt](img-url) or image URL
        if title.startswith("!") or "/images/" in url or image_ext_re.search(url):
            continue
        if url_counts[url] >= 3:
            continue
        if url_filter_re and not url_filter_re.search(url):
            continue
        if _is_non_job_url(url):
            continue
        title = re.sub(r"\s*\\+\s*", " ", title)
        title = title.strip("* ").strip()
        title = _sanitize_board_title(title)
        if _has_dept_count_suffix(title):
            continue
        if title.startswith("Operations") and not _looks_like_job_title(title):
            continue
        if _looks_like_job_title(title) and title not in seen_titles:
            seen_titles.add(title)
            location = _extract_location_near(markdown, match.start(), match.end(), job_url=url)
            snippet = _extract_snippet_near(markdown, match.end())
            # Skip junk: UI elements captured as "jobs" have tiny snippets
            if len(snippet) < 50:
                continue
            if _is_bio_snippet(snippet):
                continue
            if _is_careers_boilerplate(snippet):
                continue
            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "department": "",
                    "url": url,
                    "external_id": hashlib.md5(url.encode()).hexdigest()[:12],
                    "snippet": snippet,
                }
            )

    heading_pattern = re.compile(r"^(?:#{1,4}\s+|\*\*)(.*?)(?:\*\*)?$", re.MULTILINE)
    for match in heading_pattern.finditer(markdown):
        title = match.group(1).strip().strip("*").strip()
        title = _sanitize_board_title(title)
        if _has_dept_count_suffix(title):
            continue
        if (
            _looks_like_job_title(title)
            and title not in seen_titles
            and len(title) > 10
            and len(title) < 120
        ):
            seen_titles.add(title)
            location = _extract_location_near(markdown, match.start(), match.end())

            # Look forward for URL + snippet (GovAI / Webflow pattern:
            # # Job Title \n description... \n [Read more](url))
            lookahead = markdown[match.end() : match.end() + 600]
            next_h = re.search(r"^(?:#{1,4}\s+|\*\*)", lookahead, re.MULTILINE)
            if next_h:
                lookahead = lookahead[: next_h.start()]
            url = ""
            # Find all links in lookahead; pick first that passes url_filter
            for link_m in re.finditer(r"\[([^\]]+)\]\((https?://[^\)]+)\)", lookahead):
                candidate = link_m.group(2).strip()
                if image_ext_re.search(candidate):
                    continue
                if url_filter_re and not url_filter_re.search(candidate):
                    continue
                if _is_non_job_url(candidate):
                    continue
                url = candidate
                break
            snippet = _extract_snippet_near(markdown, match.end())

            if not url and len(snippet) < 50:
                continue
            if _is_bio_snippet(snippet):
                continue

            ext_id = (
                hashlib.md5(url.encode()).hexdigest()[:12]
                if url
                else hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12]
            )
            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "department": "",
                    "url": url,
                    "external_id": ext_id,
                    "snippet": snippet,
                }
            )

    return jobs


# Pre-compiled patterns for staff-bio detection (BUG-8).
# A scraped people/leadership page turns a staff bio into a phantom "vacancy":
# the snippet reads as prose ABOUT a named person who already HOLDS the title,
# not a role being advertised. Each pattern targets the start of the snippet,
# where a bio leads with the person's name. Deliberately case-SENSITIVE and
# name-shaped: job-ad boilerplate ("The successful candidate has managed…",
# "The Director is responsible for…", "the postholder has led…",
# "who has served in a similar capacity") must NOT match.
_BIO_PATTERNS = (
    # "Marieke Hounjet joined Porticus in 2017"
    re.compile(r"\b(?:[A-Z][a-z]+\s+){1,3}joined\s+\w+\s+in\s+\d{4}\b"),
    # "Katy Hartley is the Director of ..." — a PERSON name (2+ title-case
    # words, not a sentence starter like "The Director is responsible…" and
    # not an org blurb like "Acme Foundation is a leading nonprofit…")
    # followed by "is the/a/an/our" + a Capitalized role word.
    re.compile(
        r"^(?!(?:The|This|Our|Each|Every|All|As|At|In|On)\b)"
        r"[A-Z][a-z]+(?:\s+[A-Z][a-z.'-]+){1,3}\s+is\s+(?:the|an?|our)\s+[A-Z][a-z]+"
    ),
    # "... Katy has worked ...", "She has led ...", "He has served ..." —
    # a named person (Capitalized) or a literal lowercase pronoun narrating
    # career history. No IGNORECASE: "candidate has managed" / "postholder
    # has led" / "who has served" are standard job-ad phrasing, not a bio.
    re.compile(
        r"\b(?:[A-Z][a-z]+|she|he|they)\s+ha(?:s|ve)\s+"
        r"(?:worked|led|joined|served|held|spent|overseen|managed|founded)\b"
    ),
)


# Emoji / pictograph / dingbat ranges — presence in a "title" marks a
# fabricated about-page fragment, not a real vacancy (BUG-8).
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # symbols, pictographs, emoji, supplemental
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U00002190-\U000021ff"  # arrows
    "\U00002300-\U000023ff"  # technical (⌂ ⏻ …)
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0001f1e6-\U0001f1ff"  # regional indicators
    "]"
)


def _is_bio_snippet(snippet: str) -> bool:
    """Detect team-member biography snippets that are NOT job listings.

    Catches a staff bio scraped from a people/leadership page in place of a
    real posting, e.g. "Katy Hartley is the Director of Organisational Strategy
    and Regional Networks for Porticus. In the last 25 years, Katy has worked…"
    (BUG-8) or "Marieke Hounjet joined Porticus in 2017". Only the opening of
    the snippet is inspected — a bio leads with the person, a job describes the
    role.
    """
    head = (snippet or "")[:200]
    return any(p.search(head) for p in _BIO_PATTERNS)


def _looks_like_job_title(text: str) -> bool:
    """Heuristic: does this text look like a job title?

    Uses structural heuristics (generic, future-proof) + targeted negative
    phrases to reject FAQ artifacts, employee bios, navigation links, etc.
    Only affects Firecrawl/markdown parsing — for ALL-source blocking,
    use GLOBAL_BLACKLIST in config.py instead.
    """
    text = text.strip()
    text_lower = text.lower()

    # --- Structural heuristics (reject before keyword check) ---
    # Emoji / pictograph in a "title" is a strong phantom signal: the company
    # scrape fabricated a fragment from about-page decoration, e.g.
    # "🏛legitimacy provider" / "💪🔌🏰connection to power" (BUG-8). Real job
    # titles are plain text.
    if _EMOJI_RE.search(text):
        return False
    if text.endswith("?"):
        return False
    if len(text.split()) > 8:
        return False
    if "http" in text_lower or "www." in text_lower:
        return False

    # Reject bare plural role categories / talent pools:
    # "(Senior) Directors", "Managers", "Officers" — not real vacancies
    stripped = re.sub(r"\([^)]+\)\s*", "", text).strip()
    if re.fullmatch(r"[A-Z][a-z]+s", stripped):
        return False

    # --- Keyword check ---
    job_keywords = [
        "manager",
        "director",
        "head",
        "lead",
        "officer",
        "coordinator",
        "analyst",
        "associate",
        "specialist",
        "engineer",
        "developer",
        "designer",
        "advisor",
        "consultant",
        "researcher",
        "assistant",
        "vice president",
        "vp",
        "chief",
        "senior",
        "junior",
        "intern",
        "partner",
        "recruiter",
        "administrator",
        "strategist",
        "program",
        "programme",
        "project",
        "product",
        "operations",
    ]
    # Word-boundary match (allowing plural): a bare substring check let
    # "international" match "intern" and "Projected" match "project",
    # importing research articles as vacancies.
    has_keyword = any(re.search(rf"\b{re.escape(kw)}s?\b", text_lower) for kw in job_keywords)

    # Reject pure "FirstName LastName" pattern (2-3 capitalized words, no job keyword)
    words = text.split()
    if 2 <= len(words) <= 3 and not has_keyword:
        if all(w[0].isupper() and w[1:].islower() for w in words if len(w) > 1):
            return False

    # --- Negative phrases ---
    skip_phrases = [
        "about us",
        "our team",
        "benefits",
        "culture",
        "values",
        "how to apply",
        "submit",
        "sign up",
        "log in",
        "cookie",
        "privacy",
        "terms",
        "home",
        "menu",
        "search",
        "filter",
        "all jobs",
        "all positions",
        "clear",
        "reset",
        "partners",
        "advisory board",
        "our leadership",
        "our people",
        "partner with us",
        "read more about",
        "recent posts",
        "get funding",
        "donate to",
        "our funds back",
        "product tours",
        "product tagging",
        "enroll now",
        "talent directory",
        "general applications",
    ]
    is_navigation = any(
        re.search(rf"\b{re.escape(phrase)}\b", text_lower) for phrase in skip_phrases
    )

    return has_keyword and not is_navigation


def _extract_location_near(text: str, start: int, end: int, job_url: str = "") -> str:
    """Try to find a location string near a job title in markdown."""
    if job_url:
        workday_match = re.search(r"/job/([A-Za-z][A-Za-z\s\-]+)/[A-Z]", job_url)
        if workday_match:
            city = workday_match.group(1).replace("-", " ").strip()
            if (
                city
                and len(city) < 40
                and not any(x in city.lower() for x in ["job", "http", "www", "career", "en-us"])
            ):
                return city

        ebrd_match = re.search(r"/job/([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)-[A-Z]", job_url)
        if ebrd_match:
            city = ebrd_match.group(1).strip()
            if city and len(city) < 25:
                return city

    context = text[end : end + 300]
    if any(bad in context[:50] for bad in ["](https:", "](http:", "](//", "[!["]):
        context = ""

    if not context:
        return ""

    loc_patterns = [
        r"(?:Location|Office|Based in|\u2022)[:\s]+([A-Za-z][^\n\[\]()\u2022|]{2,50}?)(?:\s*[\u2022|\n]|$)",
        r"\b((?:Remote|Hybrid|On-site)[^|\n\[\]()]{0,40})",
        _LOCATION_HINT_RE,
    ]
    for pat in loc_patterns:
        m = re.search(pat, context, re.IGNORECASE)
        if m:
            loc = m.group(1).strip().rstrip(".,;")
            if any(bad in loc for bad in ["](", "[!", "http", "\\", "*", "#"]):
                continue
            if len(loc) < 40:
                return loc
    return ""


def _extract_snippet_near(text: str, end: int, max_chars: int = 200) -> str:
    """Extract a short descriptive snippet from markdown text after a job link."""
    context = text[end : end + 600]
    context = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", context)
    context = re.sub(r"<br\s*/?>", " ", context, flags=re.IGNORECASE)
    context = re.sub(r"<[^>]{1,60}>", "", context)
    context = re.sub(r"[*_#`\\]", "", context)
    context = re.sub(r"\n{2,}", "\n", context)
    lines = []
    for line in context.split("\n"):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        if _looks_like_job_title(line) and len(lines) > 0:
            break
        if re.match(
            r"^(?:Full time|Part time|Remote|Hybrid|On-site|\d{2}|\w+,\s*\w{2}$)",
            line,
            re.IGNORECASE,
        ):
            continue
        lines.append(line)
        if sum(len(l) for l in lines) > max_chars:
            break

    snippet = " ".join(lines).strip()
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rsplit(" ", 1)[0] + "\u2026"
    return snippet


def _parse_json_jobs(
    json_data: dict, org_name: str, base_url: str, *, url_filter: str = ""
) -> list[dict]:
    """Convert Firecrawl JSON extraction output to standard vacancy dicts.

    Args:
        url_filter: optional regex pattern — only URLs matching this pattern are accepted.
                    Used to prevent capturing external links (e.g. GovAI → governance.ai/post/).
    """
    if not json_data or not isinstance(json_data, dict):
        return []
    raw_jobs = json_data.get("jobs", [])
    if not isinstance(raw_jobs, list):
        return []
    url_filter_re = re.compile(url_filter) if url_filter else None
    # Blocks pulled from an about/team/people/leadership page are staff bios far
    # more often than real postings — note it so a rejection log is legible.
    from_people_page = _is_people_page_url(base_url)
    src_note = " [people-page source]" if from_people_page else ""
    jobs = []
    seen_titles = set()
    for j in raw_jobs:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        if not title or not _looks_like_job_title(title):
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        job_url = (j.get("url") or "").strip()
        # Resolve relative URLs
        if job_url and not job_url.startswith("http"):
            job_url = urllib.parse.urljoin(base_url, job_url)
        # Apply url_filter — skip external links that don't match the pattern
        if url_filter_re and job_url and not url_filter_re.search(job_url):
            continue
        if job_url and _is_non_job_url(job_url):
            continue
        # BUG-8 guard: the body reads as a biography of the person who already
        # HOLDS the title (e.g. the Porticus leadership-bio phantom), not a role
        # being advertised. Reject regardless of URL; log so a wrong drop shows.
        snippet = (j.get("snippet") or "").strip()
        if _is_bio_snippet(snippet):
            print(f"  [{org_name}] rejected non-posting (staff bio): {title!r}{src_note}")
            continue
        # BUG-8 guard: a URL-less block scraped from an about/team/people/
        # leadership page is page chrome or a bio, never an apply-able posting.
        # (A URL-less block from a real careers/jobs page is left alone — some
        # boards list roles without a per-role link.)
        if not job_url and from_people_page:
            print(f"  [{org_name}] rejected non-posting (no apply URL): {title!r}{src_note}")
            continue
        jobs.append(
            {
                "title": title,
                "location": (j.get("location") or "").strip(),
                "department": (j.get("department") or "").strip(),
                "url": job_url,
                "external_id": hashlib.md5((job_url or f"{org_name}:{title}").encode()).hexdigest()[
                    :12
                ],
                "snippet": snippet,
            }
        )
    return jobs


def _filter_by_keywords(items: list, keywords: list[str], title_fields: list[str]) -> list:
    """Keep only items where any keyword matches any title field."""
    if not keywords:
        return items
    result = []
    for item in items:
        combined = " ".join(str(item.get(f, "")) for f in title_fields).lower()
        if any(kw.lower() in combined for kw in keywords):
            result.append(item)
    return result


def _blacklist_filter(
    items: list,
    blacklist: list[str],
    title_fields: list[str],
    substr_blacklist: list[str] | None = None,
) -> list:
    """Remove items where any blacklist term matches any title field.

    blacklist terms are matched with word boundaries.
    substr_blacklist terms are matched as plain substrings (for partial stems like 'nutritio').
    """
    if not blacklist and not substr_blacklist:
        return items
    bl = [kw.lower() for kw in blacklist]
    bl_sub = [kw.lower() for kw in (substr_blacklist or [])]
    result = []
    for item in items:
        combined = " ".join(str(item.get(f, "")) for f in title_fields).lower()
        if any(kw in combined for kw in bl_sub):
            continue
        if any(re.search(r"\b" + re.escape(kw) + r"\b", combined) for kw in bl):
            continue
        result.append(item)
    return result


def _is_generic_pipeline_title(title: str) -> bool:
    """Return True for non-actionable posting shells (EOI/talent pool/general app)."""
    title_lower = (title or "").lower()
    return any(re.search(pattern, title_lower) for pattern in GENERIC_PIPELINE_TITLE_PATTERNS)


def _location_filter(
    items: list,
    locations: list[str],
    city_field: str = "tags_city",
    country_field: str = "tags_country",
) -> list:
    """Keep only items where any location tag matches the target list."""
    if not locations:
        return items
    targets = [loc.lower() for loc in locations]
    result = []
    for item in items:
        cities = [c.lower() for c in (item.get(city_field) or [])]
        countries = [c.lower() for c in (item.get(country_field) or [])]
        all_locs = cities + countries
        if any(tgt in loc for loc in all_locs for tgt in targets):
            result.append(item)
    return result


_TITLE_KEYWORDS = {
    "Manager",
    "Director",
    "Associate",
    "Coordinator",
    "Consultant",
    "Advisor",
    "Grants",
    "Officer",
    "Specialist",
    "Analyst",
    "Lead",
}
_CITY_PREFIX_RE = re.compile(
    r"^(San Francisco|New York|Los Angeles|Washington|London|Berlin|Chicago|Boston|Houston|Seattle)",
    re.I,
)
_PRICE_RE = re.compile(r"USD\s*\d|EUR\s*\d|\$\d")


def _is_parsing_artifact(candidate: str) -> bool:
    """Reject candidates that are job titles, cities, or price strings — not org names."""
    if _PRICE_RE.search(candidate):
        return True
    if _CITY_PREFIX_RE.match(candidate):
        return True
    first_word = candidate.split()[0] if candidate.split() else ""
    if first_word.rstrip(",") in _TITLE_KEYWORDS:
        return True
    return False


def _extract_org_from_listing(snippet: str, title: str, board_name: str) -> str:
    """Try to extract hiring org name from a job board listing.
    Falls back to '[via BoardName]' if extraction fails.
    """
    # Pattern: "at OrgName" in title
    m = re.search(r"\bat\s+([A-Z][A-Za-z0-9 &.,\-]+)", title)
    if m:
        candidate = m.group(1).strip().rstrip(".,")
        if (
            3 < len(candidate) < 60
            and not re.search(r"\d", candidate)
            and not _is_parsing_artifact(candidate)
        ):
            return candidate

    # Pattern: bold org before pipe or dash in snippet
    m = re.match(r"\*{0,2}([A-Z][A-Za-z0-9 &.,\-]+?)\*{0,2}\s*[|\-]", snippet)
    if m:
        candidate = m.group(1).strip()
        if (
            3 < len(candidate) < 60
            and not re.search(r"\d", candidate)
            and not _is_parsing_artifact(candidate)
        ):
            return candidate

    return f"[via {board_name}]"

#!/usr/bin/env python3
"""Single quality gate for any text written to vacancy.full_description.

Every path that persists a job description (ATS fetch, board merge, Devex /
LinkedIn import, blind re-enrichment) must run the text through
``clean_description()`` first. The gate does two things:

  1. Strips a cookie/consent banner — leading (before the JD) or trailing
     (a Cookiebot/OneTrust widget rendered after the JD, the common shape
     for a Firecrawl only_main_content scrape) — so it never poisons the LLM
     scoring window (the impactpool incident: an ~11.5K-char consent wall
     overwrote 149 real descriptions because the check lived in one script;
     a leading-only strip alone still let trailing-banner descriptions
     through, since the banner sits after — not before — the real content).
  2. Rejects pages that are nothing but boilerplate — a pure cookie wall, an
     ATS "job gone" error page, or raw navigation chrome — so they are never
     saved as if they were a real JD.

Keep this module dependency-free (stdlib only) so it can be imported from the
DAL, the importers, and the enrichers without dragging in Firecrawl, psycopg2,
or the company registry.
"""

import re

# ---------------------------------------------------------------------------
# Cookie / consent boilerplate detection
# ---------------------------------------------------------------------------

# Markers that OPEN a cookie-consent banner. Deliberately site-speak only
# ("we use cookies", "this website uses...") — phrases like "cookie policy"
# or "consent" alone are excluded so a privacy/GDPR-role JD never anchors.
_COOKIE_BANNER_RE = re.compile(
    r"we use cookies|we value your privacy|we care about your privacy|"
    r"this (?:web)?site (?:uses|makes use of) cookies|accept all cookies|"
    r"by clicking [\"“]?accept|cookies (?:and other identifiers|to enhance)",
    re.IGNORECASE,
)
# Weak markers used only to extend an already-detected banner (consent-manager
# vendor lists repeat "cookie"/"consent" every few hundred chars).
_COOKIE_WORD_RE = re.compile(r"\bcookies?\b|\bconsent\b|\bgdpr\b", re.IGNORECASE)
COOKIE_HEAD_WINDOW = 1500  # banner must START this close to the top
COOKIE_CHAIN_GAP = 700  # max gap between cookie words inside one banner
COOKIE_MIN_REMAINDER = 500  # less real content than this after the banner → cookie wall
COOKIE_SCORE_POLLUTION = 1000  # banner ≥ this ate the LLM scoring window → rescore


def _find_cookie_banner_end(text: str):
    """Return end offset of a leading cookie-consent block, or None.

    Anchors on a strong marker near the top, then chains weak cookie words
    (consent-manager vendor lists mention "cookie" every few hundred chars),
    so a single vague "cookie" inside a legit JD never triggers stripping.

    Requires little/no real content BEFORE the anchor. On a long document a
    strong marker within the head window is always a genuine leading banner
    (nothing of substance precedes it). On a SHORT document (real JD under
    ``COOKIE_HEAD_WINDOW`` chars) a TRAILING widget's anchor can also land
    inside that window — that's not a leading banner, it's real content
    followed by a widget, so this returns None for it and lets
    ``_strip_trailing_cookie_banner`` handle it instead.
    """
    m = _COOKIE_BANNER_RE.search(text, 0, COOKIE_HEAD_WINDOW)
    if not m:
        return None
    if len(text[: m.start()].strip()) >= COOKIE_MIN_REMAINDER:
        return None
    end = m.end()
    while True:
        nxt = _COOKIE_WORD_RE.search(text, end, min(end + COOKIE_CHAIN_GAP, len(text)))
        if not nxt:
            break
        end = nxt.end()
    # Extend to the nearest paragraph/line boundary so we don't cut mid-sentence
    para = text.find("\n\n", end)
    if para != -1 and para - end < 300:
        return para
    line = text.find("\n", end)
    if line != -1 and line - end < 300:
        return line
    return end


def strip_cookie_boilerplate(text: str) -> str:
    """Remove a leading cookie-consent banner; return text unchanged if none."""
    if not text:
        return text
    end = _find_cookie_banner_end(text)
    if end is None:
        return text
    return text[end:].strip()


def _strip_trailing_cookie_banner(text: str) -> str:
    """Remove a cookie-consent block appended AFTER the real content.

    ``_find_cookie_banner_end`` only looks for a banner opening near the top
    (a LEADING banner, bounded by ``COOKIE_HEAD_WINDOW``). Some sites instead
    render the consent widget's markdown at the END of the scraped page — a
    Cookiebot/OneTrust widget injected late in the DOM, which a Firecrawl
    ``only_main_content`` scrape happily includes after the real job text
    (observed live: most polluted descriptions had the banner in the final
    third of the text, not the first).

    Reuses the same anchor list as the leading-banner check — no head-window
    restriction this time, since real content already precedes the match:
    everything from the first anchor hit to the end is the banner block.
    """
    if not text:
        return text
    m = _COOKIE_BANNER_RE.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip()


def is_cookie_boilerplate(text: str) -> bool:
    """True when scraped text is essentially a cookie/consent page.

    A banner is present AND almost no real content follows it — the page
    needs JS to render the actual job description. (``_find_cookie_banner_end``
    already rules out a match that has substantial real content before it,
    so this only sees genuine leading banners.)
    """
    if not text:
        return False
    end = _find_cookie_banner_end(text)
    if end is None:
        return False
    return len(text[end:].strip()) < COOKIE_MIN_REMAINDER


# ---------------------------------------------------------------------------
# Error / dead page detection
# ---------------------------------------------------------------------------

# Phrases that identify an HTTP error / "job gone" page rather than a JD.
# Anchored to the head of the text so a JD that merely mentions "404" in a
# code sample is not rejected. UNOPS gone-jobs 302-redirect to a
# /careersmarketplace/Error page; PageUp gone-jobs land on a "jobnotfound"
# listing — both render the same kind of apology copy below.
_ERROR_PAGE_RE = re.compile(
    r"\b(?:404 not found|page not found|error 404|http error|"
    r"access denied|forbidden|cannot be displayed|"
    r"the page you (?:are looking for|requested) (?:cannot be found|"
    r"does not exist|no longer exists)|"
    r"this (?:job|position|vacancy|posting) (?:is no longer|has been|"
    r"is not) (?:available|filled|open|active)|"
    r"job not found|no longer accepting applications|"
    r"this opportunity (?:is no longer available|has (?:closed|expired)))\b",
    re.IGNORECASE,
)
ERROR_HEAD_WINDOW = 600  # error copy must appear this close to the top
ERROR_MAX_LEN = 1200  # a real JD with a "no longer available" footer is longer


def is_error_page(text: str) -> bool:
    """True when the text is an HTTP-error / 'job gone' page, not a JD.

    Requires the error phrase near the top AND the whole page to be short:
    a genuine JD is long and any "no longer available" line lives in a footer,
    not in the opening 600 chars.
    """
    if not text:
        return False
    if len(text.strip()) > ERROR_MAX_LEN:
        return False
    return _ERROR_PAGE_RE.search(text, 0, ERROR_HEAD_WINDOW) is not None


# ---------------------------------------------------------------------------
# Navigation chrome / empty-shell detection
# ---------------------------------------------------------------------------

# Short link-list tokens that dominate a scraped nav menu / page shell.
# Chrome-specific only: content words that occur in real JDs ("privacy",
# "cookie policy", "search", "share", "news", "jobs") are excluded so a
# short privacy/comms JD never gets classified as nav junk.
_NAV_TOKEN_RE = re.compile(
    r"\b(?:home|about(?: us)?|contact(?: us)?|menu|login|log in|"
    r"sign in|sign up|register|careers|terms of (?:use|service)|"
    r"skip to (?:main )?content|toggle navigation|"
    r"subscribe|newsletter|follow us)\b",
    re.IGNORECASE,
)
NAV_MAX_LEN = 600  # nav shells are short
NAV_TOKEN_THRESHOLD = 6  # this many nav tokens in a short blob → it's chrome


def is_navigation_junk(text: str) -> bool:
    """True when the text is mostly navigation chrome, not a description.

    Only fires on short blobs (a real JD is long); counts nav-menu tokens and
    flags the text when they dominate the content.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) > NAV_MAX_LEN:
        return False
    return len(_NAV_TOKEN_RE.findall(stripped)) >= NAV_TOKEN_THRESHOLD


# ---------------------------------------------------------------------------
# Raw page-script detection (length-independent)
# ---------------------------------------------------------------------------

# Raw inline-JavaScript / DOM-manipulation signatures. A markdown/text scrape
# of a real JD never carries these; a page shell whose scripts leaked into the
# text (an ATS listing page rendered when the job detail was gone, e.g. PageUp
# careers pages whose inline jQuery html2text couldn't strip) is full of them.
# LENGTH-INDEPENDENT on purpose: the nav/error/cookie detectors above all cap
# at a short length, so a LONG chrome dump (5K+ chars of jQuery + nav) falls
# through every one — it would otherwise be saved as a description and scored
# on the title alone.
_SCRIPT_JUNK_RE = re.compile(
    r"jQuery\s*\(|addEventListener\s*\(|<!\[CDATA\[|"
    r"document\.(?:getElementById|querySelector|cookie|write)|"
    r"window\.(?:addEventListener|location|onload)|"
    r"\.classList\.(?:add|remove|toggle|contains)|"
    r"\btypeof\s+\w+\s*[!=]=|function\s*\w*\s*\([^)]*\)\s*\{",
    re.IGNORECASE,
)
# ≥ this many raw-JS markers → the text is a script/chrome dump, not a JD. Set
# above 1 so a dev JD that mentions a single "function()" or "addEventListener"
# in prose is never misread as chrome (verified: a real page has dozens of
# hits, a frontend JD has ≤1).
SCRIPT_JUNK_THRESHOLD = 3


def is_script_junk(text: str) -> bool:
    """True when the text is dominated by raw inline page scripts, not a JD.

    Unlike is_navigation_junk / is_error_page (both capped to short blobs), this
    fires at any length — the failure it guards is a LONG page shell whose
    inline JavaScript leaked into the scrape.
    """
    if not text:
        return False
    return len(_SCRIPT_JUNK_RE.findall(text)) >= SCRIPT_JUNK_THRESHOLD


def is_boilerplate_junk(text: str) -> bool:
    """True when text is pure page boilerplate — a cookie wall, an ATS
    error/'job gone' page, navigation chrome, or a shell dominated by inline
    page scripts — rather than a real job description.

    The single boolean the pre-score blind gate shares with clean_description's
    pre-checks, so scoring rejects the same junk the save gate does (defence in
    depth: it also catches a junk row already persisted before this gate
    existed).
    """
    return (
        is_cookie_boilerplate(text)
        or is_error_page(text)
        or is_navigation_junk(text)
        or is_script_junk(text)
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Verdicts returned by clean_description():
#   "ok"          — usable description (possibly with a banner stripped)
#   "empty"       — no usable text at all
#   "cookie_wall" — a cookie/consent page with no real content behind it
#   "error_page"  — an HTTP-error / 'job gone' page
#   "nav_junk"    — navigation chrome / empty page shell
#   "too_short"   — real-looking but below the minimum usable length

MIN_DESCRIPTION_CHARS = 100  # below this a description is not worth saving


def clean_description(text: str, *, min_chars: int = MIN_DESCRIPTION_CHARS):
    """Quality gate for any text headed for vacancy.full_description.

    Returns ``(cleaned_text | None, verdict)``:
      - ``(text, "ok")`` — usable; a leading cookie banner (if any) is stripped.
      - ``(None, reason)`` — rejected; reason is one of the verdict strings above.

    Pure function, no I/O. Call this BEFORE comparing lengths or writing.
    """
    if not text or not text.strip():
        return None, "empty"

    # Pure boilerplate pages — reject outright, nothing real behind them.
    if is_cookie_boilerplate(text):
        return None, "cookie_wall"
    if is_error_page(text):
        return None, "error_page"
    if is_navigation_junk(text) or is_script_junk(text):
        return None, "nav_junk"

    # Real content with a leading and/or trailing consent banner — strip
    # both, keep the JD.
    cleaned = strip_cookie_boilerplate(text).strip()
    cleaned = _strip_trailing_cookie_banner(cleaned)

    if len(cleaned) < min_chars:
        return None, "too_short"

    return cleaned, "ok"

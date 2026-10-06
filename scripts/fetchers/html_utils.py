"""HTML → text / snippet / markdown / multiline helpers.

Pure functions shared by every adapter: entity decoding, tag stripping,
snippet trimming, compensation/deadline extraction, markdown conversion.
"""

import html as html_module
import re

# _html_to_multiline now lives in quality.py — the description gate needs it
# too, and importing this package from there would drag in requests and the
# SQLite connection. Re-exported so every adapter keeps its existing import.
from quality import _html_to_multiline  # noqa: F401


def _html_to_text(html: str) -> str:
    """Decode HTML entities and strip tags, return clean text."""
    if not html:
        return ""
    html = html.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    html = html.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _html_to_snippet(html: str, max_chars: int = 400) -> str:
    """Strip HTML tags and return a clean text snippet."""
    text = _html_to_text(html)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "\u2026"
    return text


def _extract_compensation(raw_html: str) -> str:
    """Extract monthly compensation from job description HTML.
    Returns a human-readable string like '\u20ac4,700-\u20ac6,100/mo' or '' if not found.
    """
    text = _html_to_text(raw_html)
    if not text:
        return ""

    # Pattern 1: "Compensation: \u20acX,XXX - \u20acY,YYY" (a common highlights layout)
    m = re.search(
        r"[Cc]ompensation[:\s]+([\u20ac$\u00a3][\d,]+(?:\.\d+)?)\s*[-\u2013]\s*([\u20ac$\u00a3]?[\d,]+(?:\.\d+)?)",
        text,
    )
    if m:
        return _format_monthly(m.group(1), m.group(2))

    # Pattern 2: "OTE (On-Target Earnings): $X - $Y"
    m = re.search(
        r"OTE[^:]*:\s*([\u20ac$\u00a3][\d,]+(?:\.\d+)?)\s*[-\u2013]\s*([\u20ac$\u00a3]?[\d,]+(?:\.\d+)?)",
        text,
    )
    if m:
        return _format_monthly(m.group(1), m.group(2))

    # Pattern 3: "base pay range... $X-$Y" (CZI style)
    m = re.search(
        r"(?:base pay|salary|pay)\s+range[^\u20ac$\u00a3]*([\u20ac$\u00a3][\d,]+(?:\.\d+)?)\s*[-\u2013]\s*([\u20ac$\u00a3]?[\d,]+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    if m:
        return _format_monthly(m.group(1), m.group(2))

    # Pattern 4: "Compensation: X,XXX Serbian dinars" or "GEL X,XXX"
    m = re.search(
        r"[Cc]ompensation[:\s]+([\d,]+(?:\.\d+)?)\s*[-\u2013]\s*([\d,]+(?:\.\d+)?)\s*(Serbian dinars|GEL|dinars)",
        text,
    )
    if m:
        lo = _parse_number(m.group(1))
        hi = _parse_number(m.group(2))
        currency = "GEL" if "GEL" in m.group(3) else "RSD"
        return f"{currency} {lo:,.0f}-{hi:,.0f}/mo"

    # Pattern 5: "GEL11,000 - GEL14,050"
    m = re.search(r"(GEL)([\d,]+)\s*[-\u2013]\s*(?:GEL)?([\d,]+)", text)
    if m:
        lo = _parse_number(m.group(2))
        hi = _parse_number(m.group(3))
        return f"GEL {lo:,.0f}-{hi:,.0f}/mo"

    # Pattern 6: single salary "Salary: \u20acX,XXX"
    m = re.search(r"(?:[Ss]alary|[Cc]ompensation)[:\s]+([\u20ac$\u00a3][\d,]+(?:\.\d+)?)", text)
    if m:
        return _format_monthly(m.group(1), None)

    return ""


def _extract_deadline(raw_html: str) -> str:
    """Extract application deadline from job description HTML."""
    text = _html_to_text(raw_html)
    if not text:
        return ""
    m = re.search(
        r"(?:[Dd]eadline|[Cc]losing\s+date|[Aa]pply\s+by|[Aa]pplications?\s+close)[:\s]+([A-Za-z0-9,\s]+\d{4})",
        text,
    )
    if m:
        return m.group(1).strip()
    return ""


def _parse_number(s: str) -> float:
    """Parse '5,100' or '5100.50' to float."""
    return float(s.replace(",", ""))


def _format_monthly(lo_str: str, hi_str: str | None) -> str:
    """Format salary range as monthly. Assumes input is already monthly
    unless the number is > 20,000 (likely annual).
    """
    currency = ""
    for c in ["\u20ac", "$", "\u00a3"]:
        if c in lo_str:
            currency = c
            break

    lo = _parse_number(lo_str.replace(currency, ""))
    hi = _parse_number(hi_str.replace(currency, "")) if hi_str else None

    if lo > 20000:
        lo = lo / 12
        if hi:
            hi = hi / 12

    if hi:
        return f"{currency}{lo:,.0f}-{currency}{hi:,.0f}/mo"
    return f"{currency}{lo:,.0f}/mo"


def _strip_site_chrome(soup) -> None:
    """Drop nav, header, footer, aside and forms from a parsed page.

    A chrome tag that holds a large share of the page's text is kept: some
    sites wrap the whole page in ``<header>`` (found live on a Webflow site).
    """
    total = len(soup.get_text()) or 1
    for tag in soup(["nav", "header", "footer", "aside", "form"]):
        if not tag.decomposed and len(tag.get_text()) / total < 0.4:
            tag.decompose()


_BLOCK_TAGS = ("p", "div", "br", "tr", "section", "article", "ul", "ol", "table", "dt", "dd")


def _html_to_markdown(html: str, base_url: str = "", *, main_only: bool = False) -> str:
    """Convert raw HTML to markdown with bs4: links (made absolute against
    ``base_url``), headings and list items are kept, everything else is text.
    ``main_only`` drops the site chrome (nav, header, footer, aside) first,
    like Firecrawl's ``only_main_content``.

    This is the free stand-in for Firecrawl's markdown, so it must keep links:
    ``parse_markdown_jobs`` finds vacancies by their ``[title](url)`` links.
    """
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    if main_only:
        _strip_site_chrome(soup)
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not href.startswith("http"):
            continue
        # A job card is often one link wrapping a heading plus details: the
        # heading is the title, the rest becomes the text after the link.
        head = a.find(re.compile(r"^h[1-6]$"))
        text = " ".join((head or a).get_text(" ").split())
        rest = ""
        if head is not None:
            head.extract()
            rest = " ".join(a.get_text(" ").split())
        if text:
            a.replace_with(f" [{text.replace(']', ')')}]({href.replace(' ', '%20')}) \n{rest}\n")
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        h.insert_before(f"\n\n{'#' * int(h.name[1])} ")
        h.insert_after("\n\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = html_module.unescape(soup.get_text())
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _absolutize_links(html: str, base_url: str) -> str:
    """Rewrite root-relative href="/..." links to absolute URLs."""
    from urllib.parse import urljoin

    return re.sub(r'(href=")(/[^"]+)', lambda m: m.group(1) + urljoin(base_url, m.group(2)), html)

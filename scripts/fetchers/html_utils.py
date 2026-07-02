"""HTML → text / snippet / markdown / multiline helpers.

Pure functions shared by every adapter: entity decoding, tag stripping,
snippet trimming, compensation/deadline extraction, markdown conversion.
"""

import html as html_module
import re


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


def _html_to_markdown(html: str) -> str:
    """Convert raw HTML to markdown using html2text (links + structure kept)."""
    try:
        import html2text
    except ImportError:
        # Last-resort: crude tag strip so parse_markdown_jobs still sees links.
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)
        return html_module.unescape(re.sub(r"\s+", " ", text))
    h = html2text.HTML2Text()
    h.body_width = 0  # no hard wrapping
    h.ignore_images = True
    h.ignore_emphasis = True
    h.single_line_break = True
    return h.handle(html)


def _absolutize_links(html: str, base_url: str) -> str:
    """Rewrite root-relative href="/..." links to absolute URLs."""
    from urllib.parse import urljoin

    return re.sub(r'(href=")(/[^"]+)', lambda m: m.group(1) + urljoin(base_url, m.group(2)), html)


def _html_to_multiline(html_text: str) -> str:
    """Strip HTML but keep paragraph/list structure as newlines.

    Unlike _html_to_text (which collapses everything to one line), this keeps
    descriptions readable for LLM scoring: <br>, </p>, </div> become newlines,
    <li> becomes a bullet.
    """
    if not html_text:
        return ""
    t = html_module.unescape(html_text)
    # Both opening and closing block tags break the line (HN comments often
    # start the body with an opening <p> right after the header line).
    t = re.sub(r"(?i)<\s*/?\s*(?:br|p|div|h[1-6]|ul|ol)(?:\s[^>]*)?\s*/?\s*>", "\n", t)
    t = re.sub(r"(?i)<\s*li\b[^>]*>", "\n- ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

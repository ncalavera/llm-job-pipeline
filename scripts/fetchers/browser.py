"""Local headless browser (Playwright Chromium): the free renderer for JS pages.

One browser per process, one page at a time, closed at exit. Images, fonts and
media are never downloaded. ``render_html`` returns "" when Playwright or its
browser is missing, so a caller treats "no browser" like "page not rendered".

Install once per machine: ``pip install playwright`` and
``python -m playwright install chromium-headless-shell``.
"""

import atexit
import re

from fetchers.html_utils import _absolutize_links
from fetchers.http import _LOCAL_UA

_state: dict = {}
_SKIP_RESOURCES = ("image", "media", "font")


def _browser():
    if "browser" not in _state:
        from playwright.sync_api import sync_playwright

        _state["pw"] = sync_playwright().start()
        # ponytail: one shared browser, serial pages. forge has 8 GB and froze on
        # parallel browser sessions (24.09); go parallel only on a bigger box.
        _state["browser"] = _state["pw"].chromium.launch(
            headless=True, args=["--disable-dev-shm-usage", "--disable-gpu"]
        )
        atexit.register(close)
    return _state["browser"]


def close() -> None:
    """Stop the shared browser (safe to call twice)."""
    for key, stop in (("browser", "close"), ("pw", "stop")):
        obj = _state.pop(key, None)
        try:
            if obj is not None:
                getattr(obj, stop)()
        except Exception:
            pass


def render_html(url: str, *, wait_ms: int = 2500, timeout_ms: int = 45000) -> str:
    """Load ``url`` in the browser and return the rendered HTML.

    Embedded frames (an ATS widget in an iframe) are appended, with their
    root-relative links made absolute against the frame's own address.
    """
    if _state.get("unavailable"):
        return ""
    try:
        browser = _browser()
    except Exception as e:
        # Asked once per run: a missing browser does not come back mid-run.
        print(f"  Browser unavailable ({type(e).__name__}: {str(e)[:120]})")
        close()
        _state["unavailable"] = True
        return ""
    # The browser's own version in a desktop User-Agent: "HeadlessChrome" and an
    # ageing Chrome version are both refused by some bot walls.
    major = browser.version.split(".")[0]
    context = browser.new_context(
        user_agent=re.sub(r"Chrome/\d+", f"Chrome/{major}", _LOCAL_UA),
        locale="en-US",
        viewport={"width": 1366, "height": 2400},
    )
    try:
        page = context.new_page()
        page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in _SKIP_RESOURCES
                else route.continue_()
            ),
        )
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # a page that keeps polling never goes idle; the wait below covers it
        page.wait_for_timeout(wait_ms)
        html = page.content()
        for frame in page.frames[1:]:
            try:
                if frame.url.startswith("http"):
                    html += "\n" + _absolutize_links(frame.content(), frame.url)
            except Exception:
                pass
        return html
    except Exception as e:
        print(f"  Browser render failed for {url}: {type(e).__name__}: {str(e)[:200]}")
        return ""
    finally:
        try:
            context.close()
        except Exception:
            pass

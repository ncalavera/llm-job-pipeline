"""Shared HTTP skeleton for all fetchers.

One place for the request/error boilerplate every adapter used to copy:

* ``get()`` / ``post()`` resolve the HTTP module through ``fetchers.requests``
  at CALL time, so tests can swap in a fake with one monkeypatch.
* Failures raise a typed :class:`FetchError` whose ``reason`` distinguishes a
  timeout from an HTTP status from a generic network error — the raw material
  for honest ``fetch_status`` values ("отказ ≠ пусто").
"""

import requests as _real_requests

# Browser-like User-Agent so sites don't serve a bot/blank page.
_LOCAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    """A fetch failed for a known reason (timeout / HTTP status / network).

    ``reason`` is a short machine-readable code ("timeout", "http_500",
    "network"); ``status`` is the ready-to-store fetch_status string
    ("error: timeout"). ``detail`` keeps the human-readable message.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)

    @property
    def status(self) -> str:
        return f"error: {self.reason}"


def _http():
    """Resolve the requests module through the package (monkeypatch-friendly)."""
    import fetchers

    return fetchers.requests


def _classify(exc: Exception) -> str:
    """Map a transport exception to a FetchError reason code."""
    if isinstance(exc, (_real_requests.exceptions.Timeout, TimeoutError)):
        return "timeout"
    if "timed out" in str(exc).lower():
        return "timeout"
    return "network"


def request(method: str, url: str, *, check: bool = True, **kwargs):
    """Perform an HTTP request, raising FetchError on failure.

    ``kwargs`` are forwarded verbatim to ``requests.<method>`` (params,
    headers, timeout, json, data, cookies, …). With ``check=True`` (default)
    a >=400 response raises ``FetchError("http_<code>")``; pass ``check=False``
    to inspect the raw response yourself (redirect probing, retry loops).
    """
    http = _http()
    try:
        resp = getattr(http, method)(url, **kwargs)
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(_classify(exc), f"{exc}") from exc
    if check:
        try:
            resp.raise_for_status()
        except Exception as exc:
            code = getattr(resp, "status_code", None)
            reason = f"http_{code}" if isinstance(code, int) and code >= 400 else "http_error"
            raise FetchError(reason, f"{exc}") from exc
    return resp


def get(url: str, *, check: bool = True, **kwargs):
    return request("get", url, check=check, **kwargs)


def post(url: str, *, check: bool = True, **kwargs):
    return request("post", url, check=check, **kwargs)

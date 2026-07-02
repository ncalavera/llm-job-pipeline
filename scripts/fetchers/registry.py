"""Strategy registry + per-source failure recording.

Adding a source = one new file in ``fetchers/ats/`` or ``fetchers/boards/``
decorated with :func:`company_fetcher` / :func:`board_fetcher`. The decorators
do three jobs:

1. register the strategy in ``COMPANY_FETCHERS`` / ``BOARD_FETCHERS`` so the
   pipeline can dispatch on the config's ``strategy`` string;
2. wrap the adapter in the shared error boundary: a failure prints, records
   its reason for ``fetch_status`` and returns ``[]`` — an adapter never
   crashes the whole run (same contract as the old monolith);
3. keep the failure distinguishable from a genuinely empty source via
   :func:`get_fetch_errors` ("отказ ≠ пусто").
"""

import functools
from typing import Callable

from fetchers.http import FetchError

# strategy string → callable(org_name, config) -> list[dict]
COMPANY_FETCHERS: dict[str, Callable] = {}

# strategy string → callable(board_cfg) -> list[dict]
BOARD_FETCHERS: dict[str, Callable] = {}


def record_fetch_error(source: str, status: str) -> None:
    """Remember why ``source`` failed this run (status like "error: timeout")."""
    import fetchers

    fetchers._last_fetch_errors[source] = status


def get_fetch_errors() -> dict[str, str]:
    """Return per-source failure reasons recorded during this run."""
    import fetchers

    return dict(fetchers._last_fetch_errors)


def _guard(fn: Callable, source_of: Callable) -> Callable:
    """Shared error boundary: print, record the reason, return []."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        source = source_of(args)
        try:
            return fn(*args, **kwargs)
        except FetchError as exc:
            print(f"  [{source}] ERROR: {exc.detail or exc.reason}")
            record_fetch_error(source, exc.status)
            return []
        except Exception as exc:  # parity with the monolith: never crash the run
            print(f"  [{source}] ERROR: {exc}")
            record_fetch_error(source, f"error: {exc}")
            return []

    return wrapper


def company_fetcher(fn: Callable) -> Callable:
    """Error boundary for company/ATS fetchers: ``fn(org_name, ...)``."""
    return _guard(fn, lambda args: args[0] if args else fn.__name__)


def board_fetcher(strategy: str) -> Callable:
    """Register + guard a board fetcher: ``fn(board_cfg) -> list[dict]``."""

    def deco(fn: Callable) -> Callable:
        wrapped = _guard(
            fn,
            lambda args: (
                (args[0].get("name") if args and isinstance(args[0], dict) else None) or strategy
            ),
        )
        BOARD_FETCHERS[strategy] = wrapped
        return wrapped

    return deco


def register_company(strategy: str) -> Callable:
    """Register a company-strategy entry point: ``fn(org_name, config)``."""

    def deco(fn: Callable) -> Callable:
        COMPANY_FETCHERS[strategy] = fn
        return fn

    return deco

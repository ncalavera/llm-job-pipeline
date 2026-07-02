# Contributing

The most common contribution is a new vacancy source. This guide covers it
end-to-end; for everything else, keep changes small, neutral by default, and
green on `pytest` + `ruff` (see the checklist at the bottom).

## Adding your own job board or ATS

Fetchers live in the `scripts/fetchers/` package. One source = one file;
modules self-register by strategy name, so you never edit the pipeline to add
a source.

```
scripts/fetchers/
  http.py        # shared HTTP skeleton: get/post + typed FetchError
  registry.py    # strategy registries + error boundary
  html_utils.py  # HTML → text/snippet/markdown helpers
  parsing.py     # markdown/JSON job parsing, blacklist filters
  firecrawl.py   # Firecrawl scraper + local fallbacks
  ats/           # one file per ATS provider
  boards/        # one file per job board
```

### The job dict

Every fetcher returns `list[dict]` with at least `title`, `location`,
`department`, `url`, `external_id`, `snippet`. Optional: `full_description`,
`compensation`, `deadline`. Board fetchers also set `org_override` (the real
employer name) and `org_url`.

### Add a job board (one new file)

Create `scripts/fetchers/boards/exampleboard.py`:

```python
"""Example board (public JSON API, no auth)."""

import hashlib

from fetchers import http
from fetchers.registry import board_fetcher


@board_fetcher("exampleboard_api")          # ← the strategy name
def fetch_exampleboard_board(board_cfg: dict) -> list[dict]:
    resp = http.get("https://example-board.test/api/jobs", timeout=20)
    jobs = []
    for j in resp.json().get("jobs", []):
        jobs.append(
            {
                "title": j["title"],
                "location": j.get("location", ""),
                "department": "",
                "url": j.get("url", ""),
                "external_id": str(j.get("id"))
                or hashlib.md5(j["url"].encode()).hexdigest()[:12],
                "snippet": j.get("summary", ""),
                "org_override": j.get("company", ""),
                "org_url": board_cfg["url"],
            }
        )
    print(f"  [{board_cfg['name']}] Example board: {len(jobs)} jobs")
    return jobs
```

Then declare the board in `config/defaults.toml` and enable it:

```toml
[boards.exampleboard]
name = "Example Board"
strategy = "exampleboard_api"   # must match the @board_fetcher name
url = "https://example-board.test/jobs"
tier = "C"
ttl_days = 3
free = true
board_blacklist = []
```

```bash
JOB_BOARDS=exampleboard python3 scripts/fetch_vacancies.py --boards-only
```

That's it — `boards/__init__.py` auto-imports every module in the folder, the
registry maps the strategy string to your function, and `fetch_vacancies.py`
dispatches through the registry. No other file changes.

### Add an ATS provider

Same pattern in `scripts/fetchers/ats/`, with two decorators: the public
function keeps a provider-shaped signature behind `@company_fetcher` (the
error boundary), and a tiny `@register_company("<strategy>")` entry unpacks
the per-company config:

```python
from fetchers import http
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_exampleats(org_name: str, slug: str) -> list[dict]:
    resp = http.get(f"https://api.example-ats.test/boards/{slug}/jobs", timeout=15)
    ...
    return jobs


@register_company("exampleats")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_exampleats(org_name, config["slug"])
```

Companies get a strategy via `/jobs-add` (auto-detection) or directly in the
database (`company.fetch_strategy = 'exampleats'`).

### Error handling: failure ≠ empty

Do NOT wrap your fetcher in a blanket `try/except → return []`. Use
`fetchers.http.get/post` and let errors propagate:

- `http.get()` raises a typed `FetchError` — reason `timeout`, `http_500`,
  `network` — instead of returning garbage.
- The `@board_fetcher` / `@company_fetcher` boundary catches it, prints,
  records the reason, and returns `[]` so one broken source never kills the
  run.
- The pipeline writes the recorded reason into the company's `fetch_status`
  (`error: timeout`, `error: http_500`, …), while a genuinely empty listing
  becomes `render_ok_zero`. A monitor can tell "broken" from "quiet".

For paginated sources, keep partial results: catch `FetchError` per page,
`raise` if you have collected nothing yet, `break` if you already have rows.

## Checklist

1. One new file in `ats/` or `boards/`; strategy name unique.
2. Neutral defaults — no personal sectors, locations, or keywords in code
   (guardrail: `tests/test_no_hardcoded_data.py`).
3. Board: add a `[boards.<id>]` block to `config/defaults.toml`.
4. Test with mocked HTTP (see `tests/test_board_fetchers.py` for the
   `FakeRequests` pattern — monkeypatch `fetchers.requests`).
5. `python3 -m pytest -q && ruff check .` green.

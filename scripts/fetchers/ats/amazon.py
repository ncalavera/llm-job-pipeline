"""Amazon Jobs public search API (free, no auth)."""

import hashlib

from fetchers import http
from fetchers.http import FetchError
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_amazon_jobs(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from Amazon Jobs search API (free, no auth).
    Endpoint: GET https://www.amazon.jobs/en/search.json?base_query=...
    Supports multiple queries (comma-separated) since the API does AND matching.
    """
    queries_raw = config.get("queries", config.get("base_query", "nonprofit,social impact"))
    queries = (
        [q.strip() for q in queries_raw.split(",")] if isinstance(queries_raw, str) else queries_raw
    )
    api_url = "https://www.amazon.jobs/en/search.json"
    limit = 100
    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    last_error: FetchError | None = None

    print(f"  [{org_name}] Amazon Jobs API: queries={queries}")
    for query in queries:
        offset = 0
        while True:
            params = {
                "base_query": query,
                "offset": offset,
                "result_limit": limit,
                "invalid_location": "false",
            }
            try:
                resp = http.get(api_url, params=params, timeout=15)
                data = resp.json()
            except FetchError as e:
                print(f"  [{org_name}] ERROR ({query}): {e}")
                last_error = e
                break

            raw_jobs = data.get("jobs", [])
            if not raw_jobs:
                break

            for j in raw_jobs:
                title = (j.get("title") or "").strip()
                if not title:
                    continue
                job_id = j.get("id_icims") or j.get("id") or ""
                ext_id = (
                    str(job_id)
                    if job_id
                    else hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12]
                )
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                job_url = f"https://www.amazon.jobs{j['job_path']}" if j.get("job_path") else ""
                desc = (j.get("description") or "").strip()
                snippet = (j.get("description_short") or "").strip()
                location = (j.get("location") or "").strip()

                all_jobs.append(
                    {
                        "title": title,
                        "location": location,
                        "department": (j.get("job_category") or "").strip(),
                        "url": job_url,
                        "external_id": ext_id,
                        "snippet": snippet,
                        "full_description": desc,
                    }
                )

            hits = data.get("hits", 0)
            offset += limit
            if offset >= hits:
                break

    if not all_jobs and last_error is not None:
        raise last_error  # every query failed — let the boundary record why

    print(f"  [{org_name}] Found {len(all_jobs)} vacancies (deduped across {len(queries)} queries)")
    return all_jobs


@register_company("amazon_jobs")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_amazon_jobs(org_name, config)

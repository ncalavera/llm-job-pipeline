"""Apple Jobs API (free, CSRF token + POST)."""

import hashlib

import fetchers as _pkg
from fetchers.http import FetchError
from fetchers.registry import company_fetcher, register_company


@company_fetcher
def fetch_apple_jobs(org_name: str, config: dict) -> list[dict]:
    """Fetch jobs from Apple Jobs API (free, CSRF token required).
    Step 1: GET /api/csrfToken → extract token + cookies
    Step 2: POST /api/role/search with JSON body
    """
    query = config.get("query", "social impact nonprofit")
    print(f"  [{org_name}] Apple Jobs API: query={query!r}")

    session = _pkg.requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: Get CSRF token
    try:
        csrf_resp = session.get("https://jobs.apple.com/api/csrfToken", timeout=15)
        csrf_resp.raise_for_status()
    except Exception as e:
        raise FetchError("network", f"CSRF token request failed: {e}") from e
    csrf_token = csrf_resp.headers.get("X-Apple-CSRF-Token", "")
    if not csrf_token:
        # Try extracting from response body/cookies as fallback
        csrf_token = csrf_resp.text.strip()
    if not csrf_token:
        print(f"  [{org_name}] ERROR: No CSRF token returned")
        return []

    all_jobs: list[dict] = []
    page = 1
    last_error: FetchError | None = None

    while True:
        payload = {
            "query": query,
            "filters": {"range": {"standardWeeklyHours": {"start": None, "end": None}}},
            "page": page,
            "locale": "en-us",
            "sort": "relevance",
        }
        try:
            resp = session.post(
                "https://jobs.apple.com/api/role/search",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Apple-CSRF-Token": csrf_token,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{org_name}] ERROR on page {page}: {e}")
            last_error = FetchError("network", str(e))
            break

        search_results = data.get("searchResults", [])
        if not search_results:
            break

        for j in search_results:
            title = (j.get("postingTitle") or "").strip()
            if not title:
                continue
            pos_id = j.get("positionId", "")
            job_url = f"https://jobs.apple.com/en-us/details/{pos_id}" if pos_id else ""
            summary = (j.get("jobSummary") or "").strip()
            locs = j.get("locations", [])
            location = (
                ", ".join((loc.get("name") or loc.get("city") or "") for loc in locs[:3])
                if isinstance(locs, list)
                else ""
            )

            all_jobs.append(
                {
                    "title": title,
                    "location": location,
                    "department": (
                        j.get("team", {}).get("teamName", "")
                        if isinstance(j.get("team"), dict)
                        else ""
                    ),
                    "url": job_url,
                    "external_id": str(pos_id)
                    if pos_id
                    else hashlib.md5(f"{org_name}:{title}".encode()).hexdigest()[:12],
                    "snippet": summary,
                }
            )

        total = data.get("totalRecords", 0)
        if len(all_jobs) >= total or not search_results:
            break
        page += 1

    if not all_jobs and last_error is not None:
        raise last_error  # nothing fetched and the search failed — record why

    print(f"  [{org_name}] Found {len(all_jobs)} vacancies")
    return all_jobs


@register_company("apple_jobs")
def _entry(org_name: str, config: dict) -> list[dict]:
    return fetch_apple_jobs(org_name, config)

import enrich_blind_vacancies as e


def test_company_rate_limit_does_not_disable_later_sources(monkeypatch, tmp_path):
    import fetchers
    from fetchers import firecrawl as f

    monkeypatch.setattr(f, "FIRECRAWL_CACHE", tmp_path)
    monkeypatch.setattr(fetchers, "_firecrawl_credits_remaining", 100)
    monkeypatch.setattr(fetchers, "_last_scrape_status", {})
    monkeypatch.setattr(fetchers, "_fetch_local_scrape", lambda *a, **kw: [])

    class Client:
        def scrape(self, *a, **kw):
            raise Exception("429 Rate Limit Exceeded: retry after 6s")

    monkeypatch.setattr(fetchers, "get_firecrawl_client", lambda: Client())
    f.fetch_firecrawl_scrape("Example", "https://example.org/careers")
    assert fetchers._firecrawl_credits_remaining == 100
    assert fetchers._last_scrape_status.get("Example") != "credit_exhausted"


def test_company_credit_exhaustion_still_disables_paid_requests():
    from fetchers.firecrawl import _is_quota_error

    assert _is_quota_error(Exception("402 Payment Required: insufficient credits"))


def test_rate_limit_error_is_retried(monkeypatch):
    monkeypatch.setattr(e.time, "sleep", lambda s: None)
    calls = []

    class Client:
        def scrape(self, url, **kw):
            calls.append(url)
            if len(calls) == 1:
                raise Exception("Rate Limit Exceeded: Failed to scrape. retry after 16s")
            return {"markdown": "Job text"}

    assert e._scrape_job_page(Client(), "https://x.org/job") == "Job text"
    assert len(calls) == 2

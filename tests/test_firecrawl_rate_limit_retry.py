import enrich_blind_vacancies as e


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

"""Golden-snapshot characterization for filter_vacancies.generate_html_report.

The report is pure rendering with no downstream reader, so its safety net is a
byte-for-byte snapshot: build a fixed set of categories, freeze the timestamp,
render, and compare to tests/fixtures/filter_report_golden.html. Regenerate the
fixture only on a deliberate report change:

    python3 tests/test_filter_report_golden.py --record
"""

import datetime as _dt
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "filter_report_golden.html"


class _FrozenDatetime:
    """Stand-in for the datetime class: only .now() is used by the report."""

    @staticmethod
    def now():
        return _dt.datetime(2026, 1, 2, 9, 30)


def _vac(org, title, *, tier="C", region="other", relevance=2, url="", loc="Berlin, Germany"):
    return {
        "org": org,
        "title": title,
        "tier": tier,
        "region": region,
        "relevance_score": relevance,
        "url": url,
        "locations": [{"location": loc, "url": url}],
    }


def _build_categories() -> dict:
    """A deterministic spread across every category the report renders."""
    return {
        "delete_blacklist": [
            ("v1", _vac("Acme Foundation", "Expression of Interest Specialist")),
            ("v2", _vac("Beta Institute", "Volunteer Coordinator")),
        ],
        "delete_junk": [("v3", _vac("Gamma Trust", "Broken Page Role"))],
        "delete_rearchived": [("v4", _vac("Delta Fund", "Old Role Again"))],
        "delete_geo": [("v5", _vac("Epsilon Org", "On-site Only Role", loc="Lagos, Nigeria"))],
        "delete_stale_blind": [
            ("v6", _vac("Zeta Group", "Stale Blind Role", url="https://zeta.test/1"))
        ],
        "reenrich_blind": [
            ("v7", _vac("Eta Labs", "Blind Role One", tier="A", url="https://eta.test/1")),
            ("v8", _vac("Theta Co", "Blind Role Two", tier="S", url="https://theta.test/2")),
        ],
        "reenrich_thin": [
            ("v9", _vac("Iota Inc", "Thin Role", tier="B", url="https://iota.test/3")),
        ],
        "ready": [
            ("v10", _vac("Kappa Org", "Ready Strategic", tier="S", region="europe", relevance=3)),
            ("v11", _vac("Lambda Org", "Ready Strong", tier="A", region="remote", relevance=3)),
            ("v12", _vac("Kappa Org", "Ready Monitor", tier="B", region="us", relevance=1)),
        ],
    }


def _render(tmp_dir: Path) -> str:
    import filter_vacancies as fv

    fv.datetime = _FrozenDatetime  # freeze the timestamp for a stable snapshot
    try:
        categories = _build_categories()
        stats = fv.compute_stats(categories)
        out = fv.generate_html_report(categories, stats, tmp_dir / "report.html")
        return out.read_text(encoding="utf-8")
    finally:
        fv.datetime = _dt.datetime


def test_generate_html_report_matches_golden(tmp_path):
    html = _render(tmp_path)
    assert FIXTURE.exists(), "run `python3 tests/test_filter_report_golden.py --record` first"
    assert html == FIXTURE.read_text(encoding="utf-8")


if __name__ == "__main__":
    import tempfile

    if "--record" in sys.argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as d:
            FIXTURE.write_text(_render(Path(d)), encoding="utf-8")
        print(f"wrote {FIXTURE}")

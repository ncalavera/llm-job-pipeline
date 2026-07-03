"""_write_fetch_stats must not lie to the publish gate.

The gate reads vacancies/fetch_stats.json to decide whether a truncated fetch
mass-archived an org's live roles. A silently-swallowed write failure would
leave a PRIOR run's fetch_stats.json on disk, and the gate would evaluate this
run against last run's numbers — masking this run's truncation. So a failed
write logs loudly AND removes any stale file (honest "absent", not stale).
"""


def test_write_fetch_stats_happy_path_persists(monkeypatch, tmp_path):
    import fetch_vacancies as fv

    stats_path = tmp_path / "fetch_stats.json"
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", stats_path)

    fv._write_fetch_stats({"orgs": {"NewCo": {"gone": 1, "live": 9}}})

    assert stats_path.exists()
    import json

    assert json.loads(stats_path.read_text(encoding="utf-8"))["orgs"]["NewCo"]["gone"] == 1


def test_write_fetch_stats_failure_removes_stale_and_warns(monkeypatch, tmp_path, capsys):
    import fetch_vacancies as fv

    stats_path = tmp_path / "fetch_stats.json"
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", stats_path)

    # A prior run's telemetry sits on disk (benign numbers)...
    stats_path.write_text('{"orgs": {"OldCo": {"gone": 40, "live": 5}}}', encoding="utf-8")

    # ...and this run's write fails: a set is not JSON-serializable, so
    # json.dumps raises inside _write_fetch_stats.
    fv._write_fetch_stats({"orgs": {"NewCo": {1, 2, 3}}})

    # The stale file is gone — the gate now reads "absent" (no signal), never a
    # previous run's numbers dressed up as this run's.
    assert not stats_path.exists()

    # And the failure was announced loudly on stderr, not swallowed.
    err = capsys.readouterr().err.lower()
    assert "fetch telemetry" in err
    assert "stale" in err


def test_write_fetch_stats_failure_without_stale_file_still_warns(monkeypatch, tmp_path, capsys):
    import fetch_vacancies as fv

    stats_path = tmp_path / "fetch_stats.json"
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", stats_path)

    # No prior file on disk; a failed write must warn without crashing.
    fv._write_fetch_stats({"orgs": {"NewCo": {1, 2, 3}}})

    assert not stats_path.exists()
    assert "fetch telemetry" in capsys.readouterr().err.lower()

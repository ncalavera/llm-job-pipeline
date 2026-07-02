"""Backend-parity suite scaffolding.

DAL characterization tests that run against BOTH backends -- SQLite (always)
and a one-shot local Postgres (only when PARITY_PG_URL is set) -- proving the
save / status / TTL / archive / migration behaviour the daily pipeline
depends on is identical, not just assumed to be. See README.md in this
directory for how to point the suite at a throwaway Postgres.

Scoped to this directory only: registering the `parity` marker here (instead
of the shared tests/conftest.py or pytest.ini) keeps this addition isolated
from the rest of the suite.
"""

import pytest

from _bootstrap import bootstrap_postgres, bootstrap_sqlite


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "parity: SQLite<->Postgres backend-parity case. The Postgres half "
        "needs PARITY_PG_URL pointed at a throwaway local Postgres and is "
        "skipped without it; the SQLite half always runs.",
    )


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path, monkeypatch):
    """DAL module bound to a freshly migrated, isolated schema.

    Parametrized so every test using this fixture runs once per backend --
    the parity check IS the fact that the same assertions hold both times.
    """
    if request.param == "postgres":
        dal = bootstrap_postgres(monkeypatch)
    else:
        dal = bootstrap_sqlite(monkeypatch, tmp_path)
    yield dal
    dal.close_conn()

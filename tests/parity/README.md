# Backend-parity suite

Characterization tests that run the SAME assertions against SQLite (simple
mode) and Postgres (full mode) to prove -- not assume -- that the DAL
surfaces the daily pipeline depends on (save/dedup, company status, job-board
TTL, archive/resurrect, schema migrations) behave identically on both
backends.

## Running

The SQLite half always runs as part of the normal suite:

    pytest -m parity

The Postgres half needs a throwaway local Postgres -- **never** point it at a
real Supabase project (the suite refuses to start against anything with
"supabase" in the URL). One-shot with docker:

    docker run --rm -e POSTGRES_PASSWORD=postgres -p 5433:5432 postgres:16

    PARITY_PG_URL=postgresql://postgres:postgres@localhost:5433/postgres \
        pytest -m parity

Without `PARITY_PG_URL` (or docker), the Postgres half of every test is
skipped with a clear reason and the SQLite half still runs -- a bare `pytest`
stays green with no setup required.

CI runs both halves on every push (see `.github/workflows/ci.yml`, the
`python-parity` job -- a Postgres service container supplies
`PARITY_PG_URL`).

## What "parity" means here

Most tests in this directory are parametrized over `backend` (`sqlite` /
`postgres`, see `conftest.py`) and assert the SAME expected outcome for both
-- a divergence shows up as one parametrized instance failing while its
sibling passes, not as a hand-written comparison.
`test_migrations_parity.py::test_shared_table_columns_match_between_backends`
is the exception: it bootstraps both backends in one test body and diffs the
resulting schema directly.

A known, tracked divergence (auto-discovered company status differs by
backend today) is marked `xfail(strict=False)` with a docstring explaining
why -- see `test_dal_parity.py`. Everything else in this directory is
expected to pass on both backends; a new xfail here should always carry the
same kind of explanation, never a silent skip.

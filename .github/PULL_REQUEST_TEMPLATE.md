<!-- Read STRATEGY.md first. Tick every box, or explain the one you can't. -->

## What & why

<!-- One or two sentences. -->

## Three goals — this change keeps the repo:
- [ ] a working daily tool (the daily loop still runs end to end)
- [ ] a reference-quality codebase (a senior engineer could read it cold)
- [ ] usable by a stranger (clone → scored dashboard, no insider knowledge)

## Eight guardrails
- [ ] 1. Neutral by default — no personal taste in shipped code/prompts/runbooks
- [ ] 2. Cloud canonical, SQLite the honest demo — no product decision keyed off `IS_SQLITE`
- [ ] 3. Cost is a feature — model tier is the dial; per-run caps stay finite
- [ ] 4. Operable without insider knowledge — README/INSTALL match code behavior
- [ ] 5. Manual run is canonical — deterministic core in Python, no scheduled routines
- [ ] 6. Never batch scoring — one vacancy = one LLM request
- [ ] 7. Simplicity budget — each new source/board/flag serves ≥2 of the 3 goals
- [ ] 8. Closed feedback loops — no silent self-edits; changes apply on approval

## Gate
- [ ] `ruff check .` and `ruff format --check .` clean; `python3 -m pytest -q` green (baseline not shrunk)
- [ ] Changes a prompt (`scripts/prompts/**` or `scripts/prompts.py`)? Run `/jobs-eval` locally and paste the line here: `Eval: agreement X% (baseline Y%)`

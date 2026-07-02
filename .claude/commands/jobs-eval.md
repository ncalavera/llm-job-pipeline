---
description: Scoring-quality check. Build a small golden set of your own vacancies labelled fits / doesn't-fit, score them with the CURRENT prompt, and get one agreement number plus a list of disagreements to fix. Any user can run it; the set is personal and never leaves your machine.
---

# /jobs-eval

Does the scorer actually agree with *you*? This command answers that with one
number. It builds a **golden set** — a few dozen of your own vacancies each
labelled `fit` or `nofit` with a short reason — scores them with the live
`scripts/prompts/vacancy-scoring.md`, and reports how often the model's verdict
matches your label, plus precision/recall at the score threshold and a readable
list of every disagreement.

Method: Hamel Husain's "Critique Shadowing"
(https://hamel.dev/blog/posts/llm-judge/). Cheap, honest, no external service.

The golden set is **personal data** (real titles, orgs, your reasons). It lives
in `evals/` (gitignored) and is never committed. Works the same in full mode
(Postgres) and simple mode (SQLite) — it reads through the same data layer.

Everything is one script: `python3 scripts/golden_set.py <subcommand>`.

---

## 1. Build the set

**Fast path — seed from your verdicts.** Every like/pass you have already made
is a label: a liked-basket status means `fit`, a passed/skipped status means
`nofit`. Seed straight from that history (read-only):

```bash
python3 scripts/golden_set.py seed --limit 50
```

Positives are usually scarce, so `seed` keeps ALL of your fit verdicts and fills
the rest with recent nofit ones — a set with no positives can't yield a
meaningful precision/recall. Probable auto-expired passes (a `passed` role whose
deadline had already lapsed) are excluded, since those aren't fit judgements.
Re-running `seed` later only appends new verdicts; it never rewrites old labels.

**Blind path — hand-label fresh vacancies.** For coverage the verdict history
lacks, label some vacancies WITHOUT seeing their score:

```bash
python3 scripts/golden_set.py template --limit 25   # writes evals/label_template.jsonl (no scores shown)
# edit each line: set "label" to fit or nofit, add a one-line "reason"
python3 scripts/golden_set.py add-template          # appends the ones you labelled
```

Check what you have at any time:

```bash
python3 scripts/golden_set.py stats
```

---

## 2. Score the set (the scoring contract)

Same rule as `/jobs-new`: **one vacancy = one request** — never batch (batching
over-scores by +20-50). Emit the payloads, score EACH independently with your
profile's scoring model, and collect `[{"id": ..., "score": ...}]`:

```bash
python3 scripts/golden_set.py emit > /tmp/eval_payloads.json
```

Each payload carries its own `system_prompt` + `user_msg` (identical to what a
real run builds) and an `id`. For each one, run a subagent, parse the JSON it
returns, and keep `{"id": <the payload id>, "score": <0-100>}`. The label is NOT
in the payload — the scorer stays blind to the ground truth. Assemble the array
into `/tmp/eval_scores.json`.

---

## 3. Measure

```bash
python3 scripts/golden_set.py measure < /tmp/eval_scores.json
```

Prints one headline (`AGREEMENT: 82% (37/45 …)`), precision / recall / F1 at the
fit threshold (default: the pipeline's `APPLYABLE_SCORE`, override with
`--threshold`), the confusion counts, and the disagreement list:

- **FN** — you liked it, the model scored it low. The prompt is missing
  something you value.
- **FP** — you passed it, the model scored it high. The prompt is rewarding
  something you don't want.

Read the disagreements nearest the threshold first — those are the cheapest
fixes. Turn a recurring pattern into a rule tweak in
`scripts/prompts/vacancy-scoring.md` or a profile filter, then re-run `measure`
to confirm the number moved. Nothing self-edits; you make the change.

`measure` also records the result (agreement %, set size/version, threshold,
measured-at) next to the golden set — the next `/jobs-new` learning review
reads it and shows your last measured agreement instead of "not yet measured".

---

## Freezing a version

The set is append-only and version-stamped: each batch that adds labels gets the
next integer version, old lines are never touched. To measure against the set as
it stood at a past version (e.g. to compare two prompt revisions on the same
frozen data), pass `--version N` to `emit` / `measure` / `stats`.

---

## Notes

- Seed labels come from real triage verdicts, which you may have made after
  seeing a score. The blind guarantee is strongest on the `template` path; the
  seed path trades a little label purity for a set you can build in one command.
- Needs a populated database with some verdicts. On an empty database `seed`
  says so and exits — build history with a few `/jobs-new` runs first.
- Never run this from a throwaway checkout expecting the set to persist —
  `evals/` lives in your working copy.
- If you override `GOLDEN_SET_DIR`, keep it pointed OUTSIDE the repo or at a
  gitignored path: the commit guards only block the default `evals/` location,
  and your golden set contains real vacancy data that must never be committed.

---
title: "Show progress for long CLI steps via background run + heartbeat file"
module: "jobs pipeline (fetch/enrich)"
date: "2026-06-24"
problem_type: developer_experience
component: development_workflow
severity: medium
tags:
  - progress
  - background-process
  - jobs-new
  - heartbeat
applies_when: "A pipeline step runs many minutes from a single CLI invocation and an assistant/runbook needs to show live progress in chat."
---

# Show progress for long CLI steps via background run + heartbeat file

## Context

`/jobs-new` step 4 (`fetch_vacancies.py`) ran 16+ minutes with zero visible
status. The script *does* print a `--- Org ---` line per source, but the runbook
called it as a **blocking foreground command** and then sat in a
`while kill -0 … sleep; done` poll loop. A foreground command's stdout is not
streamed to chat — it appears only when the command exits. So the user stared at
a `Running shell command…` spinner for 17 minutes with no idea what was
happening or whether it had hung.

## Guidance

For any long, silent step, split progress into two halves:

1. **The script writes a heartbeat.** A tiny module (`scripts/run_status.py`)
   writes `vacancies/run_status.json` atomically after each unit of work
   (`begin(stage, total)` → `step(current, done, **extra)` → `finish()`). All
   writers swallow their own errors — a broken heartbeat must never abort the
   real job. The file is gitignored runtime state.

2. **The runbook runs the step in the background and polls.** Launch with
   `run_in_background: true` (never wrap a long command in a blocking
   `while kill -0 … sleep` loop — that re-creates the freeze). Every ~20–30s,
   render one card line from the heartbeat (`scripts/run_card.py`) and post it
   to chat. Stop when `run_status.json` has `"finished": true` or the background
   task completes; then read the tail of the task output for the final summary.

Drive the denominator with `enumerate` so skipped iterations still advance the
bar to 100%:

```python
for org_idx, (org_name, config) in enumerate(filtered.items()):
    run_status.step(org_name, org_idx, new=total_new)
    ...  # skips (continue) still leave the next idx correct
run_status.finish(new=total_new)
```

## Why This Matters

The real failure was **invisibility, not slowness** — the fetch was alive and
network-bound the whole time (5s CPU over 16 min). Streaming raw `print` lines
would not have helped because foreground stdout is buffered until exit. The fix
is architectural: progress has to live in a side channel (a file) that a
separate poll can read while the work is still running.

## When to Apply

- A single CLI call dominates wall-clock time (minutes), especially network/IO
  bound (ATS fetches, Firecrawl scrapes, board crawls).
- A runbook or assistant is expected to report status to a human in chat.
- Not worth it for sub-30s steps or steps that already stream meaningfully.

## Examples

Render the card from anywhere — fetch, enrich, or a runbook one-liner for
scoring:

```bash
# fetch / enrich write the heartbeat themselves; just render it on a tick:
python3 scripts/run_card.py
# → fetch  ▕███████░░░░░░░░░▏ 18/40 · LinkedIn · +12 new · 6m02s

# scoring is driven by the assistant, so it writes the heartbeat via one-liners:
python3 -c "import sys;sys.path.insert(0,'scripts');import run_status as r;r.begin('score',$N)"
python3 -c "import sys;sys.path.insert(0,'scripts');import run_status as r;r.step('$ORG',$k)"
```

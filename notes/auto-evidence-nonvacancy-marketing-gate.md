# Evidence: non-vacancy junk gate + cleanup

## Reported example
https://llm-job-pipeline-chi.vercel.app/?vacancy=b095704d-...
Co-Develop "Head of External Engagement" — full_description was a 5.3K-char
scrape of codevelop.fund's homepage/news feed (Featured Insights, From the News
Desk, "170 subscribers", "Tap to unmute"), scored 32 as if a real JD.

## Root cause
Firecrawl `_enrich_blind_jobs` scrapes an individual job URL; when it lands on
the org homepage (or a broken careers link that redirects there) it returns
long, well-formed prose. The save gate `clean_description()` only knew
cookie/error/nav/script boilerplate — all length-capped — so homepage PROSE
read as "ok" and was persisted + scored.

## Fix (code)
- quality.py: new `is_marketing_page()` — needs >=2 distinct homepage-only
  markers (news feed / video embed / subscriber count / funding-partners) AND
  <=1 JD-structure signal. Added to `is_boilerplate_junk()` (pre-score blind
  gate) and `clean_description()` (new "marketing_page" verdict).
- database_supabase.py: `_gate_description` blanks the field on "marketing_page".
- config/defaults.toml: title-junk += "register your interest",
  "register interest", "banco de talentos".

## Validation (goal-driven)
- `is_marketing_page` over ALL 3438 live descriptions → flags exactly 1
  (Co-Develop). Zero false positives.
- New tests: QD30-35 (marketing verdict + comms-JD-with-newsletter negative +
  single-marker negative) and 2 blacklist tests. 8 new, all pass.
- Full suite: 1528 passed, 39 skipped. ruff clean.

## Data cleanup (prod Supabase, 6 rows archived)
| status_reason | title |
|---|---|
| marketing/homepage dump | Co-Develop :: Head of External Engagement |
| grant-funding homepage | EA Funds :: people, projects, non-profits... |
| programmes/about page | interface :: Our Programmes |
| talent bank | Conta Azul :: [Banco de Talentos] |
| register-your-interest EOI | ARIA :: Register Your Interest |
| nav/section heading (empty desc) | ARIA :: Programme teams |

## Not touched (deliberate)
6 rows the gate flags as boilerplate are FALSE POSITIVES — real UNICEF jobs with
leaked PageUp chrome (DHA-446), one already `applied`. Left as real vacancies.

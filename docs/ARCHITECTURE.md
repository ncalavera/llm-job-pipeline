# Архитектура

## Поток данных

```
ATS / job boards ─┐
                  ├─► fetch_vacancies.py ─► Supabase (vacancy / company)
Firecrawl scrape ─┘            │
                               ▼
                       filter_vacancies.py (mark junk, dedup)
                               │
                               ▼
                       score_vacancies.py  (Claude Opus subagents)
                               │
                               ▼
                       fetch_vacancies.py --report-only  ─► public/data.js
                                                                │
                                                                ▼
                                                       Vercel + Supabase API
                                                                │
                                                                ▼
                                                           Дашборд
```

Все данные живут в Supabase — это единственный источник истины. Локально
ничего не хранится: на машине только код плюс кэш Firecrawl
(`.firecrawl/`, gitignored).

## Модули

```
db_conn.py              # singleton подключения к Postgres
        │
        ▼
company_registry.py     # реестр компаний, разрешение алиасов
        │
        ▼
database_supabase.py    # DAL: merge / load / score / archive
        │
        ▼
fetch_vacancies.py      # оркестратор сбора (читает fetchers.py)
filter_vacancies.py     # чистка после сбора
score_vacancies.py      # LLM-скоринг
fetch_companies.py      # Firecrawl-сбор данных о компаниях
score_companies.py      # LLM-скоринг компаний
```

`config.py` импортирует символы из `company_registry.py` для обратной
совместимости — старый код, который привык брать всё из конфига,
продолжает работать.

## Таблицы

Описаны в [sql/schema.sql](../sql/schema.sql). Кратко:

**`company`** — одна строка на каноническое название. Альтернативные
имена живут в массиве `aliases TEXT[]` с GIN-индексом. Колонки:

- Идентификация: `id`, `canonical_name`, `aliases`.
- Pipeline gate: `status` (`active` / `candidate` / `inactive`),
  `status_reason`. Только `active` подаются в скоринг и дашборд.
- Источник: `fetch_strategy`, `ats_slug`, `careers_url`, `ats_config`.
- Метаданные сбора: `last_fetched`, `vacancy_count`, `fetch_status`.
- Enrichment: `about`, `mission_fit`, `alignment_score`, `enriched_at`.

**`vacancy`** — одна строка на вакансию. Дедупликация через
`dedup_hash` = `md5(lower(canonical_name|title))`. Колонки:

- Идентификация: `id`, `dedup_hash`, `company_id` (FK).
- Контент: `title`, `snippet`, `full_description`, `compensation`,
  `deadline`, `department`, `locations` (JSONB-массив).
- Триаж: `status` (`unseen` / `liked` / `passed` / `to_apply` /
  `to_research` / `to_network` / `skipped` / `applied`),
  `status_updated_at`.
- LLM: `llm_score`, `llm_reasoning`, `llm_summary`, `llm_tags`,
  `llm_hard_requirements`, `llm_scored_at`.
- Заметки: `triage` (JSONB) — куда сохраняются решения и комментарии.

## Стратегии сбора

`fetchers.py` поддерживает следующие источники:

- **API через slug:** Greenhouse, Lever, Ashby, Workable, Recruitee,
  Personio. В `company` достаточно прописать `fetch_strategy = '<ats>'`
  и `ats_slug = '<slug>'`.
- **Workday:** требует `ats_config` с полями `tenant` и `board`.
- **BambooHR:** аналогично, нужен slug компании.
- **Алгольный поиск 80,000 Hours:** настроен в `config.py`.
- **REST API ReliefWeb:** настроен в `config.py`.
- **HTML scrape через Firecrawl:** `fetch_strategy =
  'firecrawl_scrape'`, нужен `careers_url`.
- **RSS Teamtailor:** `fetch_strategy = 'teamtailor_rss'` + `ats_slug`.

Добавить новый ATS — это новая ветка в `route()` функции внутри
`fetchers.py`. Все парсеры возвращают одинаковый dict-формат, который
потом мержится `merge_vacancies()` или `merge_board_vacancies()` в DAL.

## Скоринг

Для одной вакансии скоринг устроен так:

1. `score_vacancies.py --local --limit N` — выгружает первые `N`
   несочёрных вакансий из Supabase и печатает их в stdout как JSON.
2. Claude Code orchestrator получает JSON, запускает по одному subagent
   на вакансию (1 vacancy = 1 Opus). Внутри subagent читает тот же
   prompt template, что и API-режим (через `scripts/prompts.py`).
3. Каждый subagent возвращает `{score, reasoning, tags,
   hard_requirements, short_summary, deadline}`.
4. `score_vacancies.py --save` принимает результат на stdin и пишет в
   `vacancy.llm_*` колонки.

Один промпт на скоринг = `vacancy-scoring.md` + подставленный
пользовательский профиль. Это значит, что разные бэкенды (локальные
subagents, API, удалённый CLI) видят идентичный вход — никакого drift'а.

## Дашборд

Фронтенд — статика на Vercel:

- `public/index.html` — четыре режима (`companies`, `catalog`,
  `pipeline`, `stats`).
- `public/modules/*.js` — модули UI (catalog, companies, pipeline,
  stats, helpers, api, state).
- `public/data.js` — снапшот всех вакансий и компаний, генерируется
  скриптом из Supabase.
- `api/*.js` — Vercel serverless endpoints для обновления статусов в
  реальном времени.

При запуске дашборд читает `data.js` (быстрый рендеринг), потом
подгружает свежие статусы через `/api/statuses` и
`/api/company-statuses` и обновляет UI.

## Архитектурные решения

- **Зачем `db_conn.py` отдельно от `database_supabase.py`?** Чтобы
  разорвать цикл импортов: `company_registry.py` использует `db_conn`,
  а `database_supabase.py` использует и тот, и другой.
- **Зачем `dedup_hash` md5, а не uuid?** Стабильность. Один и тот же
  заголовок у той же компании из разных источников схлопывается в одну
  запись без участия пользователя.
- **Почему именно Opus для скоринга?** Бенчмарк показал, что Sonnet
  даёт большую дисперсию по одной и той же вакансии. Opus стабильнее на
  ±2 балла, а не ±10.
- **Почему 1 вакансия = 1 subagent?** Batch-скоринг систематически
  завышает оценку на 20–50 баллов. Изоляция контекста — единственный
  способ получить честные числа.

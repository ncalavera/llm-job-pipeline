# База данных

Полный SQL — в [sql/schema.sql](../sql/schema.sql). Здесь — что важно
знать при работе.

## Таблицы

Две таблицы: `company` и `vacancy`. Всё остальное (триаж, скоры,
enrichment, метаданные сбора) живёт колонками на этих двух.

```
company  ─┬──── vacancy (через company_id FK)
          │
          ├── aliases TEXT[]        ── GIN индекс для resolve_canonical_name
          ├── status TEXT           ── active / candidate / inactive
          └── ats_config JSONB      ── сложные настройки парсера

vacancy
├── dedup_hash TEXT UNIQUE  ── md5(lower(canonical_name|title))
├── locations JSONB         ── массив {work_mode, region, country, city, url}
├── status TEXT             ── unseen / liked / passed / to_apply / ... (8)
├── llm_score INT           ── 0-100
└── triage JSONB            ── свободная форма заметок
```

## Индексы

Все вытаскивающие индексы созданы в `schema.sql`:

- `idx_company_aliases` (GIN) — для `aliases @> ARRAY['name']` в
  `resolve_company_id`.
- `idx_company_status` — фильтр в `load_vacancies`.
- `idx_vacancy_status` — критичный для `/api/statuses` (быстрый GET всех
  статусов).
- `idx_vacancy_dedup_hash` — дедуп при `merge_vacancies`.
- `idx_vacancy_llm_score` — сортировка в дашборде по убыванию скора.

## Дедупликация

Два уровня:

1. **Exact:** `dedup_hash` — стабильный md5 от
   `(canonical_name|title).lower()`. Второй раз та же вакансия из того
   же источника просто обновляет `last_seen`.
2. **Fuzzy:** `filter_vacancies.py --dedup` находит вакансии с
   `difflib.SequenceMatcher`-сходством ≥ 0.85 (порог настраивается)
   между разными источниками через `aliases`. Помечает дубликат как
   `passed` с пометкой `dup-of:<uuid>`.

## Pipeline gate

Компания попадает в скоринг и дашборд только при `status = 'active'`.

- `candidate` — новая компания, найденная парсером job board'а. Ждёт
  ручного или автоматического одобрения.
- `active` — одобрена, вакансии видны в дашборде.
- `inactive` — отвергнута, вакансии скрыты.

Автоодобрение работает по `alignment_score`: ≥ 60 → `active`, ≤ 25 →
`inactive`. Между порогами — остаются `candidate` для ручного
рассмотрения. Конкретные значения — в `auto_review_candidates()` внутри
`database_supabase.py`.

## locations[] — массив, не строка

Историческая причина: одна вакансия часто публикуется в нескольких
локациях. Раньше склеивались в одну строку («Berlin / London / Remote»),
сейчас — массив объектов:

```json
[
  {"work_mode": "hybrid", "region": "europe", "country": "Germany",
   "city": "Berlin", "url": "https://..."},
  {"work_mode": "remote", "region": "europe", "country": null,
   "city": null, "compensation": "£60-80k"}
]
```

Парсер `parse_location()` принимает любую строку и пытается извлечь
структуру. Если не получилось — поля остаются `null`.

## Статусы вакансий

Восемь значений:

| Значение | Когда ставится |
| --- | --- |
| `unseen` | По умолчанию для новых вакансий |
| `liked` | Пользователь поставил «нравится» |
| `passed` | Пользователь отказал |
| `to_apply` | Решено отправить отклик |
| `to_research` | Нужно изучить компанию глубже |
| `to_network` | Сначала найти контакт внутри |
| `skipped` | Откинут после триажа |
| `applied` | Отклик отправлен |

`status_updated_at` обновляется автоматически в API endpoints. Триггера
в SQL нет — обновляйте в коде.

## RLS

В `schema.sql` Row-Level Security оставлен **выключенным**. Логика:

- Дашборд читает данные через `/api/statuses` и `/api/company-statuses`
  — это Vercel-функции, использующие `SUPABASE_SERVICE_ROLE_KEY` (полный
  доступ). Конечный пользователь видит уже отрендеренный HTML.
- Прямой доступ из браузера к Supabase REST API не предусмотрен.

Если вы хотите дать кому-то прямой доступ через `anon` ключ — раскоментируйте
блок `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` в конце `schema.sql`.

## Egress

Supabase free tier даёт 2 ГБ исходящего трафика в месяц. Полное
описание вакансии может быть до 50 КБ. На партию из 5000 вакансий это
~250 МБ исходящего. Если упрётесь — `load_vacancies(light=True)`
опускает `full_description` (флаг есть в DAL).

## Миграции

Файл `schema.sql` идемпотентный — `CREATE TABLE IF NOT EXISTS` и
`CREATE INDEX IF NOT EXISTS` не упадут, если объекты уже есть. Менять
схему при обновлении — добавлять отдельные `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS ...` в новый файл `sql/migrations/00X_<name>.sql`. В этом
репо отдельной миграционной системы нет — Supabase SQL Editor и есть
система миграций.

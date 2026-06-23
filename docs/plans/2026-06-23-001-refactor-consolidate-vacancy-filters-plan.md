---
title: Consolidate vacancy filtering into scripts/filters.py
type: refactor
status: active
date: 2026-06-23
origin: docs/brainstorms/2026-06-23-filters-refactor-brainstorm.md
---

# ♻️ Consolidate vacancy filtering into `scripts/filters.py`

## Overview

Сейчас отсеивание вакансий размазано по пяти файлам, один и тот же чёрный список написан дважды (по-разному), а одинаковые проверки повторяются в трёх местах с расходящимися порогами. Сводим всё в один модуль `scripts/filters.py` с чистой функцией `classify_vacancy(vacancy) → причина`. Решение считается один раз, действия по причине остаются за каждым шагом конвейера.

Главное обязательство: **блок 1 не меняет поведение** — на выходе ровно те же вакансии, что и сейчас. Это машинно проверяется тестом-снимком «до/после». Осознанные правки правил — позже, блок 4.

(см. brainstorm: docs/brainstorms/2026-06-23-filters-refactor-brainstorm.md)

## Базовая логика (главный принцип — единый для всего кода)

Любая вакансия проходит **два уровня**, и рефактор должен привести к этой логике ВСЕ места кода, где сейчас отсеивание разное.

> **Где это применяется:** уровень 1 («известная/новая») — ТОЛЬКО на шаге записи (`save_vacancies`/`save_board_vacancies`), потому что только там есть входящая вакансия, которой может не быть в базе. `/filter` и `score` читают строки, которые УЖЕ в базе, — для них «известная/новая» бессмысленно; они потребляют результат `classify_vacancy` (уровень 2) + статус, своих проверок качества не держат.

**Уровень 1 — она уже в базе? (только `save_*`)**
- **Да (известная)** → НЕ классифицируем по качеству. Смотрим, какая это известная:
  - статус решённый (`liked`/`to_apply`/`to_research`/`to_network`/`applied`/`archived`) → не трогаем;
  - недавно архивирована (<90 дней) → не воскрешаем;
  - дубликат (близнец уже в базе/в этой же партии) → оставляем одну;
  - просто живая → обновляем поля (last_seen, описание если длиннее, локация).
- **Нет (новая)** → переходим на уровень 2.

**Уровень 2 — стоит ли брать новую? (`classify_vacancy`)**
Пять причин: `not_a_job` / `wrong_role` / `wrong_location` / `no_description` / `ready`.

Ключевое: качество (`classify_vacancy`) считается **только для новых**. Всё, что про «уже знаем» — это уровень 1, и он один на весь код. Никаких отдельных самодельных проверок качества в `save`/`/filter`/`score` быть не должно — они либо обращаются к `classify_vacancy` (уровень 2), либо к общим воротам уровня 1.

## Problem Statement

- `_is_blacklisted` определён **дважды**: `database_supabase.py:118` (через скомпилированный `_BLACKLIST_PATTERN`) и `score_vacancies.py:108` (свой цикл по словам). Реализации расходятся на краях (сортировка по длине vs порядок списка).
- Сторожа `_is_content_junk`, `_has_enough_content`, `_gate_description`, `_is_recently_archived` живут в `database_supabase.py`, зовутся в `merge_vacancies` (:618) **и** `merge_board_vacancies` (:753).
- География (`delete_geo`) и протухшие/тонкие (`delete_stale_blind`/`reenrich_blind`/`reenrich_thin`) живут **только** в `filter_vacancies.py:491-576`, в записи их нет.
- `score_vacancies._load_and_dedup` (:172-246) снова проверяет чёрный список (своя копия) + инлайн «слепой» пропуск + статус.
- Пороги «мало контента» **разные** на разных шагах (запись: desc<50 и snippet<50 и нет url; `/filter`: blind=url+нет desc, thin=0<desc<100; оценка: `not desc.strip()`).
- Из-за дублей непонятно, на каком шаге и каким правилом вакансию отсеяло — тяжело отлаживать.

## Proposed Solution

### Архитектура: две оси MECE

- **Ось 1 — годность компании** (`status` active/candidate/inactive/paused). Решается наверху (`company_registry`, `load_vacancies`, inactive-gate в board-merge). В `filters.py` не лезет.
- **Ось 2 — качество вакансии** — `classify_vacancy` в `filters.py`.

### `scripts/filters.py` — публичная поверхность

```python
# Чистые проверки (без побочных эффектов, без DB-курсора, без времени)
def title_words_blacklisted(title: str) -> bool        # целые слова + куски слов в названии
def description_words_blacklisted(desc: str) -> bool    # якорные фразы в описании (узкий список!)
def is_content_junk(desc: str) -> str | None           # recaptcha/donation/error/nav
def clean_description(job: dict) -> str | None          # бывш. _gate_description, БЕЗ мутации: возвращает (reason, cleaned_text)

# Главный разборщик — одна вакансия → одна причина (ось 2)
def classify_vacancy(vacancy: dict, *, check_description: bool = False) -> str
# причина ∈ {not_a_job, no_description, wrong_role, wrong_location, ready}

# Ворота (зависят от времени/соседей — НЕ категории classify)
def is_recently_archived(archived_hashes: set[str], dedup_hash: str) -> bool
def find_duplicates(vacancies: list[dict]) -> ...      # парный fuzzy, через resolve_canonical_name
```

### Таблица соответствия: 8 старых исходов → 5 причин (НЕ теряем информацию)

| Старый исход (`filter_vacancies`) | Новая причина | Примечание |
|---|---|---|
| `delete_junk` | `not_a_job` | |
| `delete_blacklist` | `wrong_role` | только по названию |
| `delete_geo` | `wrong_location` | `_all_locations_excluded()`: нет локаций ≠ исключено |
| `reenrich_blind` | `no_description` (чинится) | свежая + есть url → дозагрузить |
| `reenrich_thin` | `no_description` (чинится) | 0<desc<100 |
| `delete_stale_blind` | `no_description` (не чинится) | url+нет desc + first_seen>7д → выкинуть |
| `delete_rearchived` | — (ворота `is_recently_archived`) | не категория |
| `ready` | `ready` | |

Признак «чинится/нет» у `no_description` — отдельный флаг, считается на шаге (нужен `first_seen` + «сейчас»), не внутри чистого `classify_vacancy`.

### Действие по причине решает шаг (таблицы действий, блок 1 = как сейчас)

- `save_vacancies` (прямой ATS) выкидывает: `not_a_job`, `wrong_role`, пусто по `_has_enough_content`; **игнорирует** `wrong_location` и `no_description`-тонкие (как сегодня — запись их не проверяет, вставляет). Ворота: `is_recently_archived(include_gone=False)`. Сохраняет воскрешение archived→unseen.
- `save_board_vacancies` (доски) — то же + inactive-company gate + `include_gone=True` (как сейчас).
- `/filter` реагирует на полный набор: `not_a_job`/`wrong_role`/`wrong_location`/`no_description`(+возраст→stale/reenrich)/`recently_archived`. Защищённые статусы — первыми, пропускаются.
- `score_vacancies` доверяет тому, что отсеяно; оставляет свой `status_exclude` (passed/skipped) и already-scored. Проверку чёрного списка снимает (её делает classify раньше) — **но** прежде доказать диф-тестом, что снятие не меняет набор.

### Переименования (самоочевидность)

- `merge_vacancies` → `save_vacancies`, `merge_board_vacancies` → `save_board_vacancies`.
- `GLOBAL_BLACKLIST` → `TITLE_BLACKLIST_WORDS`; `GLOBAL_BLACKLIST_SUBSTR` → `TITLE_BLACKLIST_STEMS`; `GLOBAL_BLACKLIST_DESC_SUBSTR` → `DESCRIPTION_BLACKLIST_PHRASES`.

## System-Wide Impact

- **Interaction graph**: `fetch_vacancies.py:407,471` → `save_vacancies`/`save_board_vacancies` → `classify_vacancy` + ворота → INSERT/UPDATE. `/filter` → `classify_vacancy` (read-only отчёт). `/score` → `_load_and_dedup` → LLM.
- **Два списка чёрного списка нельзя сливать** (задокументированные грабли: ~15% ложных срабатываний — «developer» в теле описания PM-вакансии JetBrains, «ai safety» в Anthropic). Название — полный список, описание — узкие якорные фразы. (см. `job-search-2026/docs/solutions/pipeline-issues/title-vs-description-blacklist-architecture.md`)
- **Защищённые статусы** `{liked, to_apply, to_research, to_network, applied, archived}` — `classify_vacancy` про статус НЕ знает; статус-политику держит таблица действий каждого шага. Воскрешение archived→unseen в `save_vacancies` сохраняется (не конфликтует, т.к. classify статус не трогает).
- **`clean_description` без мутации**: возвращает очищенный текст, не переписывает `job` на месте. Порог обновления описания (`new > old+100`) должен видеть тот же текст, что и сейчас.
- **State lifecycle**: счётчики `skipped_archived/junk/boilerplate/resurrected` (запись) и `blacklisted/blind/candidates` (оценка) должны остаться в выводе без изменений.
- **API surface parity**: четыре точки вызова (две записи + filter + score) должны после рефактора давать тот же `(vacancy_id → исход)`.

## Acceptance Criteria

### Блок 1 — переделка без изменения поведения
- [ ] **Двухуровневая логика применена правильно** (см. «Базовая логика»): уровень 1 «известная/новая» — только в `save_vacancies`/`save_board_vacancies`; `/filter` и `score` потребляют `classify_vacancy` (уровень 2) + статус, своих проверок качества не держат. Самодельных проверок качества вне `filters.py` не осталось — проверяется грепом по `_is_blacklisted`/`_is_content_junk`/`_has_enough_content`: 0 определений вне `filters.py`.
- [ ] `scripts/filters.py` создан; `classify_vacancy` + чистые проверки + ворота перенесены.
- [ ] Старые сторожа удалены из `database_supabase.py`, `score_vacancies.py` (вторая копия `_is_blacklisted`), `filter_vacancies.py`; все импорты ведут в `filters.py`.
- [ ] **Диф-тест чёрного списка**: на отобранном наборе названий (~60, с адверсариальными пересечениями + кейсы ложных срабатываний «developer» в теле PM-вакансии JetBrains, «ai safety» в Anthropic) обе старые реализации vs новая дают идентичный результат. Не по живой базе (она недетерминирована, conftest вырезает Supabase), а курируемый корпус в `tests/`. Двухэтапно: «обе старые согласны» (вторую копию заморозить в тесте до удаления) + «новая = старой».
- [ ] **Тест-снимок «до/после»**: на отобранном наборе (~25 вакансий, по одной на класс поведения, чекинится в `tests/fixtures/`) с замороженными часами (freezegun/monkeypatch `date.today`) старый и новый код дают одинаковый `(stable_key=(org,title) → исход)` для всех четырёх точек. Не по UUID (он случайный) и не по живой базе. Чистую часть (`classify_vacancy` + проверки) гнать параметризованно без DAL, save/score — на SQLite `dal` с замороженными часами. Гео-класс требует профиль с непустым `HARD_FILTERS`, иначе `wrong_location` не сработает.
- [ ] **Характеризационные тесты на `save_board_vacancies`** (сейчас 0 покрытия) — до переименования.
- [ ] Счётчики/статистика вывода не изменились.
- [ ] Переименование протянуто во все точки: `fetch_vacancies.py`, 14 вызовов в тестах, `.agents/skills/source-command-jobs-add/SKILL.md`, `.claude/commands/jobs-add.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/DATABASE.md`.
- [ ] **Починена реальная ошибка**: перепутанные аргументы `merge_vacancies(jobs, org_name)` в `source-command-jobs-add/SKILL.md:223` (правильный порядок — `(org_name, tier, jobs)`).
- [ ] **Починить ошибку `get_archived_hashes()`**: `interval '%s days'` не подставляет число (`%s` внутри строкового литерала) → окно не работает, `/filter` сейчас считает «недавно архивирована» по всем хешам за всю историю. Исправить параметризацию + тест на TTL (хеш 100 дней назад НЕ возвращается, 10 дней назад — возвращается). Это осознанное изменение поведения `/filter` (перестанет лишнее выкидывать), зафиксировать в отчёте. Значение окна (90/180/всё время) — отдельное product-решение блока 4.
- [ ] Сверить `_ALL_CSV_NAMES` (board-merge :803) — актуально или переименовать в `_ALL_KNOWN_NAMES`.
- [ ] Навыки `fetch` и `add-source` подчищены на путь `llm-job-pipeline` (переезд).
- [ ] Все существующие тесты зелёные (`pytest`, pythonpath=scripts, SQLite-бэкенд).

### Блок 2 — backtest на текущей базе
- [ ] Прогон `classify_vacancy` по всей базе; отчёт «что выкинули бы».
- [ ] Ни одна вакансия со статусом из защищённого набора не получает причину-«выкинуть».

### Блок 3 — учёба субагентом
- [ ] Разбор лайкнутых (`to_apply`/`liked`/`to_research`/`to_network`) против **только явных** `passed` (руками).
- [ ] Opus, 1 вакансия = 1 агент (без батчинга — задокументированные грабли пере-оценки +20-50). Отдельный шаг, не внутри `classify_vacancy`.
- [ ] Предложения новых правил/слов с обоснованием на данных.

### Блок 4 — ревизия правил (единственное изменение поведения)
- [ ] Убрать мёртвое/пересекающееся, добавить найденное в блоке 3.
- [ ] Каждое изменение проверено против eval-набора (блок 5).

### Блок 5 — eval-набор
- [ ] `evals/vacancy_eval_set.jsonl`: лайкнутые + `passed` с метками + поля, что смотрит фильтр.
- [ ] Backtest и будущие правки гоняются против него автоматически.
- [ ] Сверить с DHA-255 (golden set) — слить, если дублирует.

## Опциональное (рекомендую в блок 1)
- [ ] Столбец `vacancy.filter_reason TEXT` через `sql/migrations/0001_add_filter_reason.sql` (dual-backend). `load_vacancies` использует `SELECT *` — ноль ломающихся потребителей. Даёт видимость причины на дашборде.

## Dependencies & Risks
- **Риск:** две копии чёрного списка расходятся на краях → снят диф-тестом.
- **Риск:** `save_board_vacancies` без тестов → снят характеризационными тестами до переделки.
- **Риск:** разные пороги «мало контента» по шагам → таблицы действий повторяют текущее, снимок-тест ловит расхождение.
- **Риск:** мутация `_gate_description` → выделить чистую `clean_description`, сверить записанный текст.
- **Зависимость:** Supabase `wajbrmkyrerhqztifztz` для backtest и eval-набора.

## Sources & References

### Origin
- **Brainstorm:** docs/brainstorms/2026-06-23-filters-refactor-brainstorm.md — решения: один `filters.py`, MECE две оси, `classify_vacancy`+`save_vacancies`, блок 1 без изменения поведения, 5 блоков.

### Internal (file:line)
- `scripts/database_supabase.py:108-213` (сторожа), `:618-750` (merge_vacancies), `:753-899` (merge_board_vacancies)
- `scripts/score_vacancies.py:108-119` (копия blacklist), `:172-252` (_load_and_dedup)
- `scripts/filter_vacancies.py:491-576` (classify_vacancies), `:87-90` (protected statuses, fuzzy threshold)
- `scripts/config.py:110-168` (сборка списков)
- `scripts/fetch_vacancies.py:407,471` (точки вызова записи)
- `sql/migrations/` (конвенция миграций, dual-backend)

### Documented learnings (грабли — не ломать)
- title vs description blacklist — две отдельные архитектуры (~15% ложных срабатываний при смешивании)
- word-boundary `\b` для целых слов vs substring для кусков
- 90-day re-archive cooldown (цикл re-score→re-archive = $72/прогон)
- geo как pre-score gate, `USA_ONLY_SCORE_CAP` удалён (pure-fit scoring)
- 1 вакансия = 1 субагент, батчинг = пере-оценка

## Поправки после /plan-eng-review (2026-06-23)

### Архитектурные решения (приняты)
- **1A — уровень «известная/новая» только на записи.** `/filter` и `score` потребляют `classify_vacancy` + статус (уже отражено в «Базовая логика»).
- **`clean_description` НЕ извлекаем** — чистая функция уже есть в `scripts/quality.py:180`, возвращает `(cleaned, verdict)`. `_gate_description` (database_supabase.py:151) — тонкая обёртка с мутацией. В `save_*` заменяем вызов `_gate_description(job)` на 3 строки инлайн: позвать `clean_description`, при reject-verdict обнулить `full_description`, иначе присвоить cleaned — ДО чтения `new_desc` и порога `new>old+100`. Мутация остаётся локальной в `save_*`, `classify_vacancy` побочных эффектов не имеет. Порядок кортежа — существующий `(cleaned, verdict)`, не менять.
- **Флаг `check_description` убран.** `classify_vacancy(vacancy)` без флага. Описание-чёрный-список зовётся явно `description_words_blacklisted(desc)` ТОЛЬКО на шаге score (как сейчас: `score._is_blacklisted(title, desc)` проверяет оба, а save/filter — только title). Диф-тест проверяет на уровне входа `(title, desc)`, не только title.
- **Дедуп не объединяем.** В `filters.py` переносим только нечёткий кросс-досочный матчер (`find_duplicates`, бывш. `_find_fuzzy_dupes`, 0.85, через `resolve_canonical_name`). Per-location merge остаётся в `save_*` (это семантика записи строки, не дедуп). `(org,title)`-группировка остаётся в `score` (это бухгалтерия цикла оценки). `_clean_exact_dupes`/`_pick_winner`/`_merge_fields` остаются в `filter_vacancies.py`.
- **`filter_reason` столбец — отложить из блока 1.** Многописательный производный столбец, который устаревает (роль `no_description` → `ready` после дозагрузки, а столбец врёт). Если нужна видимость — отдельным изменением: пишет только `/filter`, честное имя `filter_report_reason` + `filter_report_at`, «на момент последнего прогона фильтра».

### Защита от циклического импорта (критерий блока 1)
- [ ] `filters.py` НЕ импортирует `database_supabase`/`db_conn`/`db_backend`. `get_archived_hashes()` остаётся в `database_supabase`; вызывающие (`save_board_vacancies`, `/filter`) грузят set сами и передают в `filters.is_recently_archived(archived_hashes, dedup_hash)`. Проверка: `grep -n "^from database_supabase\|^from db_conn\|^from db_backend" scripts/filters.py` → 0.
- [ ] Граф после рефактора: `filters → config, quality` (и больше ничего из DAL). `database_supabase → … filters`. `filter_vacancies/score/enrich_blind/fetchers → filters`.

### Полный список точек `_is_blacklisted` (было занижено)
- [ ] 4 файла, не 2: определения `database_supabase.py:118`, `score_vacancies.py:108`; импортируют `enrich_blind_vacancies.py:32`, `fetchers.py:1317` (ленивый импорт). Все перецепить на `filters.py`.

### Производительность
- [ ] **Убрать N+1**: `_is_recently_archived(cur, dedup_hash)` в циклах `merge_vacancies:655` и `merge_board_vacancies:817` — по запросу на вакансию (таблица `archived_hash` ~4550 строк). Вынести `get_archived_hashes()` ОДИН раз до цикла, передавать set. Экономит 150-1500 мс на сбор.
- [ ] **Чёрный список** в `filters.py` компилировать `_TITLE_BLACKLIST_PATTERN` на уровне модуля (как `database_supabase`), не в функции. Цикл `re.search` per-keyword из `score` удалить.
- [ ] **Backtest** (блок 2) гнать `load_vacancies(light=True)` — в базе ~1871 вакансия. Сперва проверить, что `_VACANCY_LIGHT_COLUMNS` включает `snippet` и `dedup_hash`, иначе проверки junk/rearchived деградируют.

### Тесты-страховки (критические, сейчас нет — silent-failure)
- [ ] **Защищённый статус первым**: `liked`-вакансия с чёрным названием → НЕ попадает ни в один delete-исход (highest-stakes потеря данных).
- [ ] **save не выкидывает тонкие/гео**: job с `0<desc<100` и job с гео-исключением → обе строки в базе после `save_vacancies` (сегодняшний контракт).
- [ ] **classify на минимальной строке**: `{"title": "X"}` без `locations`/`full_description`/`status` → возвращает причину, не падает (легаси-строки).
- [ ] **TTL архива**: см. правку `get_archived_hashes` выше.
- [ ] **clean_description без мутации, но вызывающий присвоил**: сохранённое описание == очищенному; кейс с cookie-баннером, пересекающим порог +100.
- [ ] **`save_board_vacancies` — 9 характеризационных тестов ДО переименования** (сейчас 0 покрытия): batch-дедуп по `external_id`; inactive-company skip; `_ALL_CSV_NAMES` ветка статуса (unknown→AUTO_DISCOVERED, known→active, `[via …`→active); `include_gone=True` (контраст с save_vacancies `False`); воскрешение; merge локаций (у досок нет url-refresh ветки — зафиксировать асимметрию); boilerplate-gate считает, но не выкидывает.

### Мелкая чистка (минимальная, в тот же проход)
- [ ] Удалить мёртвую `_get_best_description` (`filter_vacancies.py:71`, не вызывается).
- [ ] `is_content_junk`: ветка `<50 chars → navigation_snippet` дублирует `_has_enough_content(min_chars=50)`. Для блока 1 (без изменения поведения) оставить как есть; пометить для блока 4. Словарь вердиктов выровнять на `quality.py` (`nav_junk`, не `navigation_snippet`) только если не меняет счётчики.
- [ ] `_ALL_CSV_NAMES` — это load-bearing ветка статуса компании (board-merge :803), не косметика. Переименовать в `_ALL_KNOWN_NAMES`, значение не менять, покрыть тестом (см. характеризационные).

### Уточнение по resurrection
- [ ] Снимок-тест обязан покрыть взаимодействие: строка одновременно `status=archived` И хеш в недавнем архиве. `save_vacancies` воскрешает archived→unseen (без окна), а `is_recently_archived` блокирует по 90-дневному окну — два разных механизма на одно событие. Асимметрия `include_gone` (direct=False, board=True) — не потерять.

## Next Steps
→ `/ce:work` (свежая сессия, отдельная рабочая копия). Порядок внутри блока 1: сперва характеризационные тесты + снимок-тест на СТАРОМ коде (зелёные) → потом перенос в `filters.py` → снимок остаётся зелёным.

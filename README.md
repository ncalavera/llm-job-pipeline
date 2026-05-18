# llm-job-pipeline

Конвейер для поиска работы с помощью LLM. Собирает вакансии из десятков
ATS и job-боардов, фильтрует мусор, скорит каждую через Claude по вашему
профилю и показывает результат в дашборде с триажем (нравится / отложить
/ откинуть).

Подходит тем, кто:

- ищет работу серьёзно и хочет покрыть 50+ компаний без рутины;
- готов потратить полчаса на настройку профиля и SQL-схемы;
- хочет, чтобы LLM сам отсеивал мусор и подсвечивал интересное.

Английский язык в скрипте нужен только для базы Supabase
— весь дашборд и все ваши настройки на русском (или на любом языке, на
котором вы напишете профиль).

## Что внутри

- **Сбор:** ATS Greenhouse, Lever, Ashby, Workable, Workday, Recruitee,
  Teamtailor, BambooHR, Personio плюс job boards 80,000 Hours и
  ReliefWeb. Дополнительно — Firecrawl для парсинга компаний без API.
- **Фильтр:** чёрный список по заголовкам, удаление дубликатов (точное и
  fuzzy сравнение), фильтр по локациям.
- **Скоринг:** Claude Opus параллельно скорит вакансии по вашему профилю
  (1 вакансия = 1 subagent). Те же критерии применяются к компаниям.
- **Дашборд:** Vercel + Supabase, четыре режима (компании / вакансии /
  триаж / гео). Статусы вакансий синхронизируются в реальном времени.
- **Скиллы:** набор слэш-команд для Claude Code (`/fetch`, `/score`,
  `/filter`, `/triage`, `/archive`, `/vac`) — пошаговый интерактив для
  каждой стадии.

## Схема потока

```mermaid
flowchart LR
    A[ATS и job boards] -->|fetch| B[(Supabase)]
    A2[Firecrawl<br/>компании] -->|enrich| B
    B -->|filter| C[Очищенные вакансии]
    C -->|score<br/>Claude Opus| D[Скоры 0-100]
    D -->|archive < 20| E[Архив]
    D --> F[Дашборд]
    F -->|триаж| G{liked / passed /<br/>to_apply / applied}
    G -->|статус| B

    style B fill:#1E40AF,color:#fff
    style D fill:#065F46,color:#fff
    style F fill:#7C2D12,color:#fff
```

Подробнее — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Быстрый старт

Нужны: Python 3.11+, Node.js 18+, аккаунт Supabase, подписка Claude Code
(Max / Team / Enterprise — для скоринга через subagent'ов). Firecrawl
платный, но необязательный — без него работает без обогащения.

### 1. Клонировать репозиторий и поставить зависимости

```bash
git clone https://github.com/your-username/llm-job-pipeline.git
cd llm-job-pipeline
pip install -r requirements.txt
cd api && npm install && cd ..
```

### 2. Создать проект в Supabase и применить схему

1. Зарегистрироваться на [supabase.com](https://supabase.com), создать
   проект (бесплатный тариф достаточно на старте).
2. Открыть SQL Editor, вставить содержимое `sql/schema.sql`, нажать Run.
3. Скопировать URL и Service Role Key из Settings → API.

### 3. Заполнить `.env`

```bash
cp .env.example .env
```

Прописать `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`.
Источник URL подключения к Postgres — Settings → Database → Session
Pooler. Ключ Anthropic API НЕ нужен — скоринг работает через
subagent'ов в Claude Code (по подписке).

### 4. Создать профиль пользователя

```bash
cp config/user_profile.example.md config/user_profile.md
```

Открыть `config/user_profile.md` и расписать секции под себя: опыт,
целевые роли, домены, исключения. Файл попадает в шаблоны промптов через
плейсхолдеры (`{{USER_PROFILE}}`, `{{TARGET_ROLES}}`,
`{{EXCLUDE_PATTERNS}}`). Подробнее — в [docs/PROMPTS.md](docs/PROMPTS.md).

### 5. Добавить компании для мониторинга

Минимум — импортировать пример из `examples/companies.example.csv`. В
Supabase SQL Editor:

```sql
COPY company (canonical_name, fetch_strategy, ats_slug, careers_url, tier, category, status, aliases)
FROM '/path/to/llm-job-pipeline/examples/companies.example.csv'
DELIMITER ',' CSV HEADER;
```

Или через `/add-source` в Claude Code (см. [docs/SKILLS.md](docs/SKILLS.md)).

### 6. Запустить конвейер

```bash
# Собрать вакансии (TTL 3-7 дней — повторный запуск пропускает свежие)
python3 scripts/fetch_vacancies.py

# Отфильтровать мусор (blacklist по заголовкам, дубликаты)
python3 scripts/filter_vacancies.py

# Скорить через Claude (Opus subagent на каждую вакансию)
python3 scripts/score_vacancies.py --local --limit 20

# Сгенерировать дашборд (выгружает все статусы в public/data.js)
python3 scripts/fetch_vacancies.py --report-only
```

### 7. Развернуть дашборд

```bash
# Установить Vercel CLI один раз
npm install -g vercel

# Деплой
vercel --prod
```

В настройках проекта Vercel прописать переменные `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `AUTH_USER`, `AUTH_PASS`.

## Использование в Claude Code

В репозитории есть набор слэш-команд (папка `.claude/commands/`). Если
запустить Claude Code из корня репозитория, эти команды появятся
автоматически:

| Команда | Что делает |
| --- | --- |
| `/fetch` | Интерактивный сбор вакансий с выбором источников и кэша |
| `/filter` | Прогон фильтра, очистка мусора |
| `/score` | Скоринг через subagent (1 вакансия = 1 параллельный запрос) |
| `/archive` | Превью + подтверждение архивирования низких скоров |
| `/triage` | Глубокий разбор «лайкнутых» вакансий, решение apply/skip/research |
| `/vac` | CLI-триаж из терминала без открытия дашборда |
| `/add-source` | Добавить новую компанию: автодетект ATS, тестовый сбор |
| `/finish-session` | Перегенерация дашборда, коммит, push в Vercel |

Полное описание — в [docs/SKILLS.md](docs/SKILLS.md).

## Документация

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — структура данных,
  модули, dataflow.
- [docs/SKILLS.md](docs/SKILLS.md) — описание всех слэш-команд.
- [docs/PROMPTS.md](docs/PROMPTS.md) — как устроены шаблоны
  scoring-промптов, какие плейсхолдеры доступны.
- [docs/DATABASE.md](docs/DATABASE.md) — таблицы, индексы, политики
  доступа.

## Стек и оплата внешних сервисов

Конвейер собран из нескольких внешних сервисов — большую часть из них
вы платите помесячно «в ритме»:

- **Claude Opus через Claude Code.** Скоринг идёт не через Anthropic
  API, а через subagent'ов внутри подписки Claude Code (Max / Team /
  Enterprise). Это значит, что вы платите фиксированную подписку и
  работаете в рамках её токен-лимита, а не по тарифу за каждый запрос.
  `score_vacancies.py --local` выгружает вакансии в stdout, а
  orchestrator в Claude Code запускает subagent на каждую и сохраняет
  обратно. Если хочется бежать через API — придётся допилить
  `score_vacancies.py` отдельным режимом (в этой версии его нет).
- **Firecrawl — платный, по подписке.** Используется для обогащения
  описаний вакансий и сбора данных о компаниях (страницы about/jobs).
  Free tier 500 скрэйпов в месяц быстро кончается на серьёзном поиске —
  обычно нужен платный план $20–80/месяц. Если не подключать
  `FIRECRAWL_API_KEY` — конвейер всё равно работает, но без обогащения.
- **Supabase.** Free tier даёт 500 МБ БД и 2 ГБ трафика — этого хватает
  примерно на 5000 вакансий с описаниями. На больших объёмах — Pro за
  $25/месяц.
- **Vercel.** Хостинг дашборда бесплатный для одного личного проекта.
- **GitHub.** Публичный репозиторий бесплатный.

Главный мысленный сдвиг: это не «one-shot LLM API call», а живой
рабочий инструмент, который ежедневно тянет данные и стоит как
подписка на пару SaaS'ов — около $25–100/месяц в зависимости от
объёма поиска.

## Лицензия

MIT — см. [LICENSE](LICENSE).

---
description: Добавить новую компанию в мониторинг. Автоматически определяет ATS (Greenhouse, Lever, Ashby, Workable, Workday), запускает тестовый сбор, добавляет в Supabase.
---

# /add-source

Добавление новой компании в pipeline.

## Шаги

1. Спросить у пользователя:
   - Название компании (каноническое — то, что будет в дашборде).
   - URL careers-страницы (если знают; если нет — попробовать угадать).
2. Запустить детектор ATS:
   ```bash
   python3 scripts/discover_ats.py --company "Имя компании" \
                                   --url "https://careers.example.com"
   ```
   Скрипт проверит, есть ли публичный API у Greenhouse, Lever, Ashby,
   Workable, Workday, Personio, BambooHR, Recruitee, Teamtailor.
3. Если ATS найден — показать пользователю, какой и сколько вакансий
   на нём сейчас открыто. Спросить подтверждение.
4. Если не найден — спросить:
   - Использовать Firecrawl scrape (`firecrawl_scrape` strategy)? Это
     потребует кредитов на каждый фетч.
   - Пропустить компанию (если careers-страница не парсится)?
5. После подтверждения — добавить в `company`:
   ```sql
   INSERT INTO company (canonical_name, fetch_strategy, ats_slug,
                        careers_url, tier, status, aliases)
   VALUES (...);
   ```
6. Запустить тестовый сбор только для новой компании:
   ```bash
   python3 scripts/fetch_vacancies.py --companies "Имя"
   ```
7. Показать результат:
   - Сколько вакансий пришло.
   - 3 примера заголовков, чтобы проверить, что это та компания.
   - Если 0 вакансий — спросить, всё ли правильно настроено.

## Если ATS не определяется

Иногда компания использует свой собственный сайт без публичного API.
Варианты:

- **Firecrawl scrape** — медленно (5-10 секунд на страницу), требует
  `FIRECRAWL_API_KEY`, но работает для большинства HTML.
- **RSS feed** — некоторые компании отдают вакансии через RSS. Найдите
  ссылку, прописывайте `fetch_strategy = 'rss'`,
  `ats_config = {"feed_url": "..."}`.
- **Пропустить** — если стоимость скрэйпа выше потенциального
  результата, лучше не добавлять.

## Алиасы

Если компания публикуется под разными названиями (например, «Wikimedia
Foundation» и «Wikipedia»), добавьте все варианты в массив `aliases`:

```sql
UPDATE company SET aliases = ARRAY['Wikipedia', 'Wikipedia Foundation']
WHERE canonical_name = 'Wikimedia Foundation';
```

Это нужно для дедупликации вакансий из разных источников.

---
description: Запустить LLM-скоринг через Claude Opus. Один subagent на вакансию (без батчей). Сохраняет score, reasoning, tags, hard_requirements, summary.
---

# /score

Скорит вакансии параллельно через subagents. По умолчанию 20 вакансий
за раз, 5 одновременно.

## Шаги

1. Прочитать `config/user_profile.md` — убедиться, что он не пустой и
   не равен `user_profile.example.md`. Если профиль не настроен —
   предупредить пользователя.
2. Спросить:
   - Сколько вакансий скорить (по умолчанию 20)?
   - Только с пустым `llm_score` (по умолчанию) или пересчитать всё
     (`--rescore`)?
3. Запустить (этап 1 — выгрузка):
   ```bash
   python3 scripts/score_vacancies.py --local --limit N
   ```
   Скрипт напечатает JSON-массив вакансий в stdout.
4. Запарсить JSON. Для каждой вакансии запустить отдельный subagent с
   `subagent_type=general-purpose`. Промпт subagent'а:
   - System prompt: `VACANCY_SCORING_PROMPT` (загруженный шаблон).
   - User message: `VACANCY_SCORING_USER_TEMPLATE` с подстановкой.
   - Subagent возвращает JSON с полями: `score`, `reasoning`, `tags`,
     `hard_requirements`, `short_summary`, `deadline`.
5. Собрать ответы в массив `[{id, score, reasoning, ...}]`.
6. Сохранить:
   ```bash
   echo '<JSON-массив>' | python3 scripts/score_vacancies.py --save
   ```
7. Показать распределение: сколько 75+, 55-74, 35-54, ниже 35.
8. Автоархив (внутри `--save`): вакансии со скором < `LLM_SCORE_THRESHOLD`
   (по умолчанию 20) и статусом `unseen` помечаются `passed`. Это
   нормально — они так и так бы не подошли.

## Критично

- **1 вакансия = 1 subagent.** Никогда не отправляйте 2-3 в одном
  промпте — это даёт систематическое завышение на 20-50 баллов.
- **member_ids**: при сохранении используйте `member_ids` из вывода
  `--local`, не свой `id`. Это UUID из Supabase, а не sequential
  number.
- **flush=True** в Python скриптах: они уже используют `print(...,
  flush=True)` для прогресса. Если в Claude Code не видно прогресса —
  проверьте, что не запущены через `subprocess` без `stdout=None`.

## Если скоринг сломался

- `Empty profile`: `config/user_profile.md` не создан или пустой.
  Скопируйте из example.
- `Anthropic API error`: проверьте `ANTHROPIC_API_KEY` в `.env`.
- `Subagent timeout`: один из subagent'ов завис. Уменьшите параллелизм
  в orchestrator'е.

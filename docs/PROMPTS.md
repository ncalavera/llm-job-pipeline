# Шаблоны промптов

Скоринг полагается на два шаблона:

- `scripts/prompts/vacancy-scoring.md` — оценивает вакансию против
  профиля.
- `scripts/prompts/company-scoring.md` — оценивает компанию.

Оба содержат плейсхолдеры в формате `{{NAME}}`, которые подменяются на
секции из `config/user_profile.md`. Загрузчик — `scripts/prompts.py`.

## Какие плейсхолдеры доступны

| Плейсхолдер | Откуда берётся | Что туда положить |
| --- | --- | --- |
| `{{USER_PROFILE}}` | секция `## USER_PROFILE` | Кто вы, опыт, навыки, языки |
| `{{TARGET_ROLES}}` | секция `## TARGET_ROLES` | Какие роли вам нужны |
| `{{EXCLUDE_PATTERNS}}` | секция `## EXCLUDE_PATTERNS` | Что исключить |
| `{{SHORT_SUMMARY_INSTRUCTION}}` | секция `## SHORT_SUMMARY_INSTRUCTION` | Как писать саммари для карточки |
| `{{OUTPUT_LANGUAGE}}` | секция `## OUTPUT_LANGUAGE` | Язык вывода (Russian / English) |
| `{{ABOUT_INSTRUCTION}}` | секция `## ABOUT_INSTRUCTION` | Как описывать компанию |
| `{{CUSTOM_CRITERION_LABEL}}` | секция `## CUSTOM_CRITERION_LABEL` | Имя дополнительного критерия |
| `{{CUSTOM_CRITERION_DESCRIPTION}}` | секция `## CUSTOM_CRITERION_DESCRIPTION` | Описание этого критерия |
| `{{CUSTOM_BOOST_FIELD}}` | секция `## CUSTOM_BOOST_FIELD` | Имя поля в ответе LLM |

## Как добавить свой плейсхолдер

1. Добавьте секцию в `config/user_profile.md`:
   ```markdown
   ## MY_NEW_FIELD

   Сюда любой текст.
   ```
2. Используйте в промпт-шаблоне: `{{MY_NEW_FIELD}}`.
3. `prompts.py` подхватит автоматически — ничего перекомпилировать не
   нужно.

Если плейсхолдер не найдётся в `user_profile.md`, он останется в тексте
как есть (так что лучше не оставлять «дыр»).

## Как часто перезапускать скоринг при изменении промпта

Когда вы меняете `user_profile.md` или сам промпт, прошлые скоры остаются
в БД старыми. Чтобы прогнать всё заново:

```sql
-- Сбросить скоры, чтобы /score прошёлся ещё раз
UPDATE vacancy SET llm_score = NULL, llm_scored_at = NULL
WHERE status = 'unseen';
```

Потом `python3 scripts/score_vacancies.py --local --limit 200`.

## Хорошие практики

- **Не пишите профиль в негативе**: «не хочу X» хуже работает, чем
  «хочу Y». Используйте `EXCLUDE_PATTERNS` для негативов, `USER_PROFILE`
  для позитивов.
- **Конкретика бьёт абстракции**: «8 лет операционных ролей, последние 3
  — на 100+ человек» работает лучше, чем «senior operations leader».
- **Перечисляйте языки с уровнями**: «Russian native, English C1,
  Spanish B2». Без этого LLM не понимает, можно ли вам в роль с
  «working language: Spanish».
- **Раз в неделю просматривайте 10 случайных скоров**: если что-то
  систематически переоценено или недооценено — добавьте правило в
  `EXCLUDE_PATTERNS` или поправьте профиль.

## Совсем другой язык

В `OUTPUT_LANGUAGE` может быть `Russian`, `English`, `Spanish`, что
угодно — LLM подстроится. На дашборде кириллица и латиница рендерятся
одинаково.

Если хотите английский intake, но русский вывод (или наоборот) — пишите
profile на любом языке, главное чтобы `OUTPUT_LANGUAGE` совпадал с тем,
что вы хотите видеть в карточках.

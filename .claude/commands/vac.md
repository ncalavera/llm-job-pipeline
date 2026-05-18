---
description: KISS-CLI для триажа вакансий из терминала без открытия дашборда. Команды list / show / mark / open / companies.
---

# /vac

Тонкий CLI поверх Supabase для повседневного триажа. Используется,
когда не хочется открывать дашборд или вы на сервере без браузера.

## Шаги

В зависимости от запроса — запустить нужную подкоманду:

| Хочу… | Команда |
| --- | --- |
| Посмотреть топ-20 по скору | `python3 scripts/vac.py list` |
| Только лайкнутые | `python3 scripts/vac.py list --status liked` |
| Только из конкретной компании | `python3 scripts/vac.py list --company "GiveDirectly"` |
| Сортировка по дате | `python3 scripts/vac.py list --sort last_seen` |
| Развёрнутое описание | `python3 scripts/vac.py show <uuid>` |
| Поменять статус | `python3 scripts/vac.py mark <uuid> --status liked` |
| Открыть URL в браузере | `python3 scripts/vac.py open <uuid>` |
| Сводка по компаниям | `python3 scripts/vac.py companies` |

## Флаги

- `--limit N` — сколько строк показать (по умолчанию 20 для `list`).
- `--status liked,unseen` — через запятую.
- `--no-website` — компании без careers_url (полезно для `/add-source`).

## Когда не использовать

- Массовые операции (>10 вакансий) — открывайте дашборд.
- Триаж с длинными заметками — открывайте `/triage`, он пишет в
  `vacancy.triage` JSONB.
- Поиск по тексту описания — нет full-text search, проще через
  Supabase SQL Editor.

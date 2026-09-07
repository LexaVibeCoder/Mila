# Дневник (Diary)

**Файл**: `core/diary/manager.py`

Дневник — записи от имени Милы. Два зеркала: SQLite (`diary_entries` в `context_buffer.db`) — источник правды, и Markdown-файл `data/daily reports/ДД.ММ.ГГГГ.md` — человекочитаемая копия на сегодня (перезаписывается целиком при каждом изменении).

## Схема

```sql
CREATE TABLE diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_number INTEGER NOT NULL,   -- сквозной счётчик (MAX+1)
    content_md TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'day_entries',  -- day_entries | reflection_entries
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

Категории (`DIARY_CATEGORIES`): `day_entries` («Day Events» — события дня) и `reflection_entries` («Thoughts and Feelings» — мысли и чувства). Неизвестная категория нормализуется в `day_entries`.

## API

| Функция | Описание |
|---------|----------|
| `add_diary_entry(content_md, category="day_entries")` | Новая запись → `{entry_id (id), entry_number, category}` + sync в daily-файл |
| `update_diary_entry(entry_id, content_md, category=None)` | Обновление по `id` (+`updated_at`), `False` если нет; sync в файл |
| `get_all_diary_entries(category=None)` | Все записи по `entry_number ASC` |
| `get_diary_markdown()` / `get_diary_entries_markdown(category=None)` | Markdown с `[NNN] (category, ID: id)` — для логов/промптов |
| `get_diary_entry_count()` | Количество записей |
| `get_today_diary_context(max_entries=30)` | Блок для system prompt (последние 30, группировка по категориям, `[ID: N]`) |
| `clear_diary()` | `DELETE FROM diary_entries` (без touched файла) |
| `save_diary_to_logs()` | Снапшот в `logs/diary_logs/diary_ГГГГММДД.md` (используется сном при коммите) |
| `load_daily_report(date_str=None)` / `list_daily_reports()` | Чтение/список файлов `data/daily reports/` |
| `_sync_to_daily_file()` | Перегенерация сегодняшнего `ДД.ММ.ГГГГ.md` |

## Форматы

Блок в system prompt (`DIARY_BLOCK`):

```
[Your personal diary for today. Each entry has an ID — use update_diary to update an existing entry:]
---
## Day Events (day_entries):
[ID: 3] текст...
## Thoughts and Feelings (reflection_entries):
[ID: 5] текст...
---
```

Файл `data/daily reports/ДД.ММ.ГГГГ.md`:

```markdown
# Personal Diary — ДД.ММ.ГГГГ

## Day Events (day_entries)

## Entry #1 — ГГГГ-ММ-ДД ЧЧ:ММ (ID: 1)

текст

---
```

## Кто пишет/читает

- Пишут: LLM через тулзы `write_diary` / `update_diary` (см. [tools.md](tools.md)); ночью — сон (анализ дневника).
- Читают: `persona.py` (`get_today_diary_context()` → `DIARY_BLOCK`, только если непусто); Observatory `GET /api/diary*` — **файлы** daily reports, не SQLite (см. [observatory.md](observatory.md)); фронтенд-страницы дневника нет.
- `runtime diary.save_processed` (по умолч. `false`) — флаг хранения, управляется со страницы Sleep в Observatory.

## Путь к данным

Директория — `settings.DAILY_REPORTS` (`data/daily reports`), создаётся лениво. Имя файла — сегодняшняя дата `strftime("%d.%m.%Y")`.

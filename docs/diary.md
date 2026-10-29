# Дневник (Diary)

**Файл:** `core/diary/manager.py`

Дневник — записи от имени Милы. Два зеркала: SQLite (`diary_entries` в
`context_buffer.db`) — источник правды, и Markdown-файл
`data/daily reports/ДД.ММ.ГГГГ.md` — человекочитаемая копия (перезаписывается
целиком при каждом изменении SQLite).

## Схема

```sql
CREATE TABLE diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_number INTEGER NOT NULL,   -- сквозной счётчик (MAX+1)
    content_md TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'day_entries',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

Категории (`DIARY_CATEGORIES`):

- `day_entries` — «Day Events», события дня;
- `reflection_entries` — «Thoughts and Feelings», мысли и чувства.

Неизвестная категория нормализуется в `day_entries`.

## API

| Функция | Описание |
|---------|----------|
| `add_diary_entry(content_md, category="day_entries")` | Новая запись → `{entry_id, entry_number, category}` + sync в daily-файл |
| `update_diary_entry(entry_id, content_md, category=None)` | Обновление по `id` (+`updated_at`), `False` если нет; sync |
| `get_all_diary_entries(category=None)` | Все записи по `entry_number ASC` |
| `get_diary_markdown()` / `get_diary_entries_markdown(category=None)` | Markdown с `[NNN] (category, ID: id)` |
| `get_diary_entry_count()` | Количество записей |
| `get_today_diary_context(max_entries=30)` | Блок для system prompt |
| `clear_diary()` | `DELETE FROM diary_entries` (файл не трогает) |
| `save_diary_to_logs()` | Снапшот в `logs/diary_logs/diary_ГГГГММДД.md`; `bool` успеха |
| `load_daily_report(date_str=None)` / `list_daily_reports()` | Чтение/список файлов |
| `_sync_to_daily_file()` | Перегенерация сегодняшнего `ДД.ММ.ГГГГ.md` |

### Важно про ежедневный файл

`_sync_to_daily_file()` записывает **все** записи из БД в файл **сегодняшней**
даты, а не фильтрует по дате создания записи. То есть файл — это снимок
текущего содержимого дневника на момент синхронизации. Исторические файлы за
прошлые даты остаются такими, какими их оставили в тот день.

## Форматы

Блок в system prompt (`get_today_diary_context`):

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

- **Пишут:** LLM через `write_diary` / `update_diary`; ночью сон анализирует
  дневник и сохраняет его в `logs/diary_logs/` перед очисткой.
- **Читают:** `persona.py` (`get_today_diary_context()` → `context/diary_block`,
  только если непусто); Observatory `GET /api/diary*` читает **файлы** daily
  reports, а не SQLite.
- Отдельной страницы дневника во фронтенде нет.
- `runtime diary.save_processed` остаётся для совместимости API, но **больше не
  управляет резервным копированием**: `commit_all()` и `rollback_all()` всегда
  вызывают `save_diary_to_logs()`, а очищают таблицу только при успешном
  бэкапе.

## Путь к данным

`DAILY_REPORTS` (`data/daily reports`) — **пробел в пути**, всегда квотировать.
Каталог создаётся лениво. Имя файла — `strftime("%d.%m.%Y")`.

## Observatory API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/diary` | Список отчётов + preview |
| GET | `/api/diary/today` | Сегодняшний файл |
| GET | `/api/diary/{date}` | Файл по дате `ДД.ММ.ГГГГ` (валидация regex) |

> Текущая особенность: счётчик `entries` в API ищет заголовок `## Запись #`, а
> `_sync_to_daily_file()` пишет `## Entry #`. Поэтому в ответах API количество
> записей может отображаться как `0`, хотя содержимое есть.

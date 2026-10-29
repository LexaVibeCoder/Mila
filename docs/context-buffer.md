# Контекстный буфер (Context Buffer)

**Файл:** `core/context/buffer.py`

Per-chat SQLite-буфер, хранящий историю сообщений и суммаризаций. Обеспечивает
токен-аварную ротацию контекста.

## Схема

Каждый чат — отдельная таблица в `context_buffer.db` (имя санитизируется:
не-алфанумерик → `_`):

```sql
CREATE TABLE [chat_{chat_id}] (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,           -- у медиа: плейсхолдер ([фото: ...], [голосовое сообщение])
    type TEXT NOT NULL CHECK(type IN ('message', 'summary')),
    source_chunk_id TEXT,            -- ссылка на chunks.chunk_id (TEXT UUID) из mila.db
    token_count INTEGER DEFAULT 0,   -- tiktoken при вставке
    role TEXT NOT NULL DEFAULT '',   -- 'user' | 'assistant' ('' у старых summary)
    kind TEXT DEFAULT 'text',        -- text | photo | voice | ...
    created_at TEXT DEFAULT (datetime('now'))  -- локальное время, см. LOCAL_TIMEZONE
);
```

Нативные байты (image/audio) в буфере **не хранятся** — только плейсхолдеры;
байты подкладываются в LLM в `ThinkPipeline._attach_media()`.

Метки времени — **локальные** (`now_local()`, `strftime("%Y-%m-%d %H:%M:%S")`):
`LOCAL_TIMEZONE` из config, иначе системная зона (Docker-safe через
`/etc/localtime`).

## API

| Функция | Описание |
|---------|----------|
| `ensure_chat_table(chat_id)` | Создать таблицу + ALTER-миграции `token_count/role/kind` |
| `push_message(chat_id, content, source_chunk_id, role, kind)` | Добавить сообщение, авто-подсчёт токенов |
| `push_summary(chat_id, summary_text)` | Добавить суммаризацию (`type='summary'`, `role=''`) |
| `get_context(chat_id, limit)` | Последние N записей хронологически (`MAX_STM_MESSAGES` = 30) |
| `get_total_entries(chat_id)` | Общее количество записей |
| `get_total_message_tokens(chat_id)` | Сумма `token_count` только `type='message'` |
| `get_context_within_token_limit(chat_id, max_tokens)` | Summary целиком + новейшие сообщения в лимите |
| `peek_oldest_messages_by_tokens(chat_id, target_tokens)` | **Только чтение** старейших ~N токенов |
| `delete_messages_by_ids(chat_id, ids)` | Удалить строки по `id` |
| `pop_oldest_messages_by_tokens(chat_id, target_tokens)` | Legacy: peek + delete атомарно |
| `pop_oldest_messages(chat_id, count)` | Legacy: удалить N старейших |
| `get_all_chat_ids()` | Имена таблиц `chat_*` |
| `get_raw_content_for_llm(chat_id, limit)` | Сырой текст: summary + latest messages |

## Сжатие контекста

Вызывается из `queue.py` **перед каждой генерацией**
(`MilaUserbot._compress_context()`):

```python
total = get_total_message_tokens(chat_id)
if total > settings.CONTEXT_MAX_TOKENS:                 # 30000
    oldest = peek_oldest_messages_by_tokens(chat_id, 5000)   # без удаления
    summary = await summarizer.generate(...)                  # 2–3 предложения
    push_summary(chat_id, summary)                            # сначала summary
    delete_messages_by_ids(chat_id, [m["id"] for m in oldest])
```

Порядок важен:

1. Проверяется сумма токенов **только сообщений** (summary в лимит не входят).
2. Старейшие сообщения **сначала читаются**, не удаляются.
3. LLM-суммаризация через ленивый `_ensure_summarizer()` — обычный `LLMClient`
   на `MAIN_LLM_MODEL` (отдельной «дешёвой» модели нет).
4. Если LLM упал или вернул пустой текст — исходные сообщения **остаются**,
   попытка повторится на следующем сообщении. Плейсхолдера-заглушки больше нет.
5. `push_summary()` вызывается **до** удаления: падение между шагами даёт
   дублирование контекста (безопасно), а не потерю.
6. `delete_messages_by_ids()` удаляет ровно те строки, которые сжимались.

## Построение истории

`MilaUserbot._build_histories()` (источник — **только** контекстный буфер, не
`chunks`):

```python
entries = get_context_within_token_limit(chat_id, settings.CONTEXT_MAX_TOKENS)
for entry in entries:
    if entry["type"] == "summary":
        block = "[Summary previous conversation:]\n{content}"
        → role="system" в обе истории
    else:
        content = f"[{created_at}] {content}"   # префикс только у message
        role = "assistant" if role == "assistant" else "user"
        → одинаковая запись в user_visible и agent_internal
```

Особенности:

- `user_visible` и `agent_internal` сейчас **идентичны** (разделение оставлено
  для будущих изменений);
- `_resolve_roles_in_histories()` — pass-through: роли уже в буфере;
- префикс времени не добавляется к summary;
- «молчаливые» ответы Милы (`content=""`) в буфер **не попадают**: при silence
  `queue.py` пишет только `chunks`, без `push_message`; в истории они
  отсутствуют;
- пустые assistant-записи, если всё же оказались в буфере, не фильтруются на
  чтении.

## Токен-аварная выборка

`get_context_within_token_limit()`:

```
1. Загрузить всё по id ASC.
2. summaries → в результат целиком; remaining = max_tokens - их токены.
3. messages от новых к старым: брать, пока влезают; на первом
   невместившемся — break (более старые не проверяются).
4. Вернуть [summaries..., отобранные messages...] хронологически.
```

Если summary съели весь лимит, `remaining` может стать отрицательным — тогда
сообщения не попадут в историю вообще.

## Настройки (`config.py`)

| Параметр | По умолч. | Описание |
|----------|-----------|----------|
| `CONTEXT_DB_PATH` | `data/context_buffer.db` | Путь к БД |
| `CONTEXT_MAX_TOKENS` | `30000` | Лимит токенов сообщений |
| `CONTEXT_SUMMARIZE_CHUNK_TOKENS` | `5000` | Сколько сжимать за раз |
| `CONTEXT_MAX_MESSAGES` | `40` | Legacy; fallback-лимит `get_context` |
| `CONTEXT_SUMMARIZE_CHUNK` | `20` | Legacy, не используется |
| `MAX_STM_MESSAGES` | `30` | Лимит `get_context()` по умолчанию |
| `LOCAL_TIMEZONE` | `""` | IANA-зона; пусто = системная |

Старых `CONTEXT_CHUNK_SIZE` / `CONTEXT_POP_CHUNKS` в коде нет.

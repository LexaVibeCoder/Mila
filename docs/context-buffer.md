# Контекстный буфер (Context Buffer)

**Файл**: `core/context/buffer.py`

Per-chat SQLite-буфер, хранящий историю сообщений и суммаризаций. Обеспечивает токен-аварную ротацию контекста.

## Архитектура

Каждый чат имеет отдельную таблицу в `context_buffer.db` (имя санитизируется: не-алфанумерик → `_`):

```sql
CREATE TABLE [chat_{chat_id}] (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,           -- у медиа: плейсхолдер ([фото: ...], [голосовое сообщение])
    type TEXT NOT NULL CHECK(type IN ('message', 'summary')),
    source_chunk_id TEXT,          -- ссылка на chunks.chunk_id (TEXT UUID) из mila.db
    token_count INTEGER DEFAULT 0, -- считается через tiktoken при вставке
    role TEXT NOT NULL DEFAULT '', -- 'user' | 'assistant' (пусто у старых summary)
    kind TEXT DEFAULT 'text',      -- text | photo | voice | ... (миграция в ensure_chat_table)
    created_at TEXT DEFAULT (datetime('now'))  -- локальное время, см. LOCAL_TIMEZONE
);
```

Нативные байты (image/audio) в буфере НЕ хранятся — только плейсхолдеры; байты текущего уведомления подкладываются в LLM отдельно (`ThinkPipeline._attach_media`).

Метки времени — **локальные** (`now_local()`, `strftime("%Y-%m-%d %H:%M:%S")`): используется `LOCAL_TIMEZONE` из config, иначе системная зона (Docker-safe через `/etc/localtime`).

## API

| Функция | Сигнатура | Описание |
|---------|-----------|----------|
| `ensure_chat_table(chat_id)` | `(chat_id: str)` | Создать таблицу чата + ALTER-миграции `token_count`/`role`/`kind` |
| `push_message(chat_id, content, source_chunk_id, role, kind)` | `role="user"`, `kind="text"` по умолч. | Добавить сообщение, авто-подсчёт токенов |
| `push_summary(chat_id, summary_text)` | — | Добавить суммаризацию (`type='summary'`, `role=''`) |
| `get_context(chat_id, limit)` | `limit=MAX_STM_MESSAGES (30)` | Последние N записей хронологически |
| `get_total_entries(chat_id)` | — | Общее количество записей |
| `get_total_message_tokens(chat_id)` | — | Сумма `token_count` только `type='message'` |
| `get_context_within_token_limit(chat_id, max_tokens)` | — | Всегда включает суммаризации, затем новейшие сообщения в лимите |
| `pop_oldest_messages_by_tokens(chat_id, target_tokens)` | — | Удалить старейшие сообщения до освобождения target_tokens, вернуть их |
| `pop_oldest_messages(chat_id, count)` | — | Удалить N старейших сообщений, вернуть их |
| `get_all_chat_ids()` | — | Имена таблиц `chat_*` |
| `get_raw_content_for_llm(chat_id, limit)` | — | Сырой текст: `[summary...]` + `[latest messages...]` |

## Сжатие контекста (Compression)

В `MilaUserbot._compress_context()` (вызывается из `queue.py` **перед каждой генерацией**):

```python
total = get_total_message_tokens(chat_id)
if total > settings.CONTEXT_MAX_TOKENS:          # 30000
    oldest = pop_oldest_messages_by_tokens(chat_id, settings.CONTEXT_SUMMARIZE_CHUNK_TOKENS)  # 5000
    summary = await summarizer.generate("Summarize ... 2-3 sentences ...", temperature=0.3, max_tokens=300)
    push_summary(chat_id, summary)
```

**Процесс**:
1. Проверка суммы токенов **только сообщений** (суммари в лимит не входят)
2. Извлечение старейших сообщений на ~`CONTEXT_SUMMARIZE_CHUNK_TOKENS` токенов (формат строк: `[ГГГГ-ММ-ДД] первые 500 символов`)
3. LLM-суммаризация через ленивый `_ensure_summarizer()` — обычный `LLMClient` на `MAIN_LLM_MODEL` (отдельной «дешёвой» модели в коде нет)
4. Ошибка/пустой ответ → плейсхолдер `[Summary previous conversation]`
5. Вставка `push_summary()` — суммари копятся и всегда отдаются первыми

## Построение истории

В `MilaUserbot._build_histories()` (источник — **только** контекстный буфер, не `chunks`):

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

Отличия от старых версий документации:
- `_resolve_roles_in_histories()` — **pass-through**: роли уже лежат в буфере (`push_message(..., role)`), кросс-ссылки с `chunks` нет.
- Молчаливые ответы ассистента (`content=""`) попадают в буфер как обычные `message` с `role="assistant"` (фильтрации пустых на чтении нет; префикс `[ts]` не добавляется к summary).
- `user_visible` и `agent_internal` сейчас **идентичны** (разделение оставлено для будущих изменений).

## Токен-аварная выборка

`get_context_within_token_limit()`:

```
1. Загрузить ВСЁ по id ASC
2. summaries → в результат целиком (лимит уменьшается на их токены,
   может уйти в минус — тогда сообщения не влезут вообще)
3. messages от новых к старым: брать, пока влезают; на первом
   невместившемся — break (старые дальше не проверяются)
4. Вернуть [суммаризации..., отобранные сообщения...] хронологически
```

## Настройки (config.py)

| Параметр | Значение по умолчанию | Описание |
|----------|----------------------|----------|
| `CONTEXT_DB_PATH` | `data/context_buffer.db` | Путь к БД |
| `CONTEXT_MAX_TOKENS` | `30000` | Лимит токенов сообщений |
| `CONTEXT_SUMMARIZE_CHUNK_TOKENS` | `5000` | Сколько токенов сжимать за раз |
| `CONTEXT_MAX_MESSAGES` | `40` | Legacy (используется только как fallback-лимит в `get_raw_content_for_llm` через `get_context`) |
| `CONTEXT_SUMMARIZE_CHUNK` | `20` | Legacy, в коде не используется |
| `MAX_STM_MESSAGES` | `30` | Лимит `get_context()` по умолчанию |
| `LOCAL_TIMEZONE` | `""` | IANA-зона для меток; пусто = системная |

Старых `CONTEXT_CHUNK_SIZE` / `CONTEXT_POP_CHUNKS` в коде нет.

## Схема БД

```
context_buffer.db
├── chat_{chat_id}  (по таблице на чат)
│   ├── id               INTEGER PRIMARY KEY
│   ├── content          TEXT
│   ├── type             TEXT ('message' | 'summary')
│   ├── source_chunk_id  TEXT (UUID chunks.chunk_id или NULL у ответов Милы)
│   ├── token_count      INTEGER
│   ├── role             TEXT ('user' | 'assistant' | '')
│   └── created_at       TEXT (локальное время)
│
└── diary_entries     (см. diary.md)
    ├── id, entry_number, content_md, category, created_at, updated_at
```

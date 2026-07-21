# Контекстный буфер (Context Buffer)

**Файл**: `core/context_buffer.py`

Per-chat SQLite-буфер, хранящий историю сообщений и суммаризаций. Обеспечивает токен-аварную ротацию контекста.

## Архитектура

Каждый чат имеет отдельную таблицу в `context_buffer.db`:

```sql
CREATE TABLE chat_{chat_id} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,           -- 'message' или 'summary'
    role TEXT NOT NULL,           -- 'user', 'assistant', или 'system' (для суммаризации)
    content TEXT NOT NULL,
    token_count INTEGER,
    source_chunk_id INTEGER,     -- ссылка на chunks.id из mila.db
    created_at TIMESTAMP
);
```

## API

| Функция | Описание |
|---------|----------|
| `ensure_chat_table(chat_id)` | Создать таблицу для чата, если нет |
| `push_message(chat_id, content, source_chunk_id)` | Добавить сообщение, авто-подсчёт токенов |
| `push_summary(chat_id, summary_text)` | Добавить суммаризацию (type='summary', role='system') |
| `get_context(chat_id, limit)` | Записи хронологически (старые первые) |
| `get_total_entries(chat_id)` | Общее количество записей |
| `get_total_message_tokens(chat_id)` | Суммарное количество токенов сообщений |
| `get_context_within_token_limit(chat_id, max_tokens)` | **Всегда включает суммаризации**, затем новейшие сообщения, влезающие в лимит |
| `pop_oldest_messages_by_tokens(chat_id, target_tokens)` | Удалить старейшие сообщения до освобождения target_tokens |
| `pop_oldest_messages(chat_id, count)` | Удалить N старейших сообщений |
| `get_all_chat_ids()` | Список всех чатов |
| `get_raw_content_for_llm()` | Сырой контент для инъекции в LLM |

## Сжатие контекста (Compression)

В `MilaUserbot._compress_context()`:

```python
async def _compress_context(self, chat_id):
    total_tokens = get_total_message_tokens(chat_id)
    if total_tokens > CONTEXT_MAX_TOKENS:  # ~30K
        old_messages = pop_oldest_messages_by_tokens(chat_id, target_tokens)
        summary = await summarizer.summarize(old_messages)
        push_summary(chat_id, summary)
```

**Процесс**:
1. Проверка общего количества токенов сообщений
2. Если превышен `CONTEXT_MAX_TOKENS`:
   - Извлечение старейших сообщений до освобождения целевого количества токенов
   - LLM-суммаризация (через `_ensure_summarizer()` — дешёвая LLM для дневного сжатия)
   - Сохранение суммаризации с `type='summary'`, `role='system'`

## Построение истории

В `MilaUserbot._build_histories()`:

```python
def _build_histories(self, chat_id):
    entries = get_context_within_token_limit(chat_id, MAX_CONTEXT_TOKENS)
    user_visible = []      # для пользователя (user + assistant)
    agent_internal = []    # для LLM (включает system-суммаризации)
    for e in entries:
        if e['type'] == 'summary':
            agent_internal.append(('system', e['content']))
        else:
            role = resolve_role(e)  # user vs assistant
            user_visible.append({'role': role, 'content': e['content']})
            agent_internal.append((role, e['content']))
    return user_visible, agent_internal
```

`_resolve_roles_in_histories()` — кросс-ссылка с `chunks` из `mila.db` для определения, кто написал сообщение (user или assistant).

## Токен-аварная выборка

`get_context_within_token_limit()` — ключевая функция:

```
1. Загрузить ВСЕ суммаризации (type='summary') — обязательны
2. Загрузить сообщения (type='message') от новых к старым
3. Пока сумма токенов ≤ max_tokens: добавлять сообщения
4. Вернуть хронологически: [суммаризации..., сообщения...]
```

Это гарантирует, что общий контекст всегда помещается в лимит LLM.

## Настройки (config.py + runtime)

| Параметр | Значение по умолчанию | Описание |
|----------|----------------------|----------|
| `CONTEXT_DB_PATH` | `data/context_buffer.db` | Путь к БД |
| `CONTEXT_MAX_TOKENS` | 30000 | Лимит токенов контекста |
| `CONTEXT_CHUNK_SIZE` | 5 | Размер чанка для сжатия |
| `CONTEXT_POP_CHUNKS` | 3 | Количество чанков для удаления |

## Схема БД

```
context_buffer.db
├── chat_{chat_id}  (по таблице на чат)
│   ├── id            INTEGER PRIMARY KEY
│   ├── type          TEXT ('message' | 'summary')
│   ├── role          TEXT ('user' | 'assistant' | 'system')
│   ├── content       TEXT
│   ├── token_count   INTEGER
│   ├── source_chunk_id INTEGER
│   └── created_at    TIMESTAMP
│
└── diary_entries     (см. diary.md)
```

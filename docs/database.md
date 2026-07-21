# Базы данных

Mila использует два SQLite-файла и FAISS-индекс.

## 1. mila.db (основная БД)

**Файл**: `data/mila.db` (путь настраивается в config.py)

Управляется модулем `core/db.py`.

### Таблицы

#### users
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    preferred_name TEXT,       -- как Мила обращается
    relationship_type TEXT,    -- 'friend', 'family', 'stranger', etc.
    sentiment REAL DEFAULT 0.0,
    appearance TEXT,           -- описание внешности
    is_bot INTEGER DEFAULT 0,
    language_code TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

#### chats
```sql
CREATE TABLE chats (
    id INTEGER PRIMARY KEY,
    type TEXT,                 -- 'private', 'group', 'supergroup'
    title TEXT,
    username TEXT,
    created_at TIMESTAMP
);
```

#### chat_members
```sql
CREATE TABLE chat_members (
    chat_id INTEGER,
    user_id INTEGER,
    joined_at TIMESTAMP,
    PRIMARY KEY (chat_id, user_id)
);
```

#### chunks (все сообщения)
```sql
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    content TEXT NOT NULL,
    llm_full_response TEXT,    -- полный JSON ответа LLM
    type TEXT DEFAULT 'message',  -- 'message', 'system', 'action'
    faiss_id INTEGER,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

#### memories (долгосрочные воспоминания)
```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    faiss_id INTEGER UNIQUE,
    user_id INTEGER,
    content TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    confidence REAL DEFAULT 0.7,
    tags TEXT,                 -- JSON массив
    source TEXT,               -- откуда получено
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_accessed TIMESTAMP,
    access_count INTEGER DEFAULT 0
);
```

#### media_logs
```sql
CREATE TABLE media_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    media_type TEXT,           -- 'photo', 'video', 'audio', 'document'
    file_id TEXT,
    caption TEXT,
    created_at TIMESTAMP
);
```

#### mila_memory_chunks (автобиографическая память)
```sql
CREATE TABLE mila_memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content_md TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    version INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

#### Таблицы сна (все в mila.db)

```sql
-- Состояние
CREATE TABLE sleep_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL,         -- AWAKE|SLEEPING|COMMITTING|SLEEP_ERROR
    stage TEXT,
    session_id TEXT,
    triggered_by TEXT,
    started_at TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(session_id)
);

-- Очередь сообщений во сне
CREATE TABLE sleep_message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    message_text TEXT,
    processed INTEGER DEFAULT 0,
    created_at TIMESTAMP
);

-- Стейдж изменений (для атомарного коммита/отката)
CREATE TABLE sleep_transaction_staging (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,  -- add_chunk|update_chunk|delete_chunk|...
    target_table TEXT NOT NULL,
    target_id INTEGER,
    old_data TEXT,                -- для отката
    new_data TEXT,                -- новые данные
    is_applied INTEGER DEFAULT 0,
    created_at TIMESTAMP
);

-- Лог суммаризаций
CREATE TABLE sleep_dialog_summary_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id INTEGER,
    summary_text TEXT,
    messages_count INTEGER,
    created_at TIMESTAMP
);
```

## 2. context_buffer.db (буфер контекста)

**Файл**: `data/context_buffer.db`

Управляется модулем `core/context_buffer.py`.

### Таблицы

Для каждого чата создаётся отдельная таблица:

```sql
CREATE TABLE chat_{chat_id} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,            -- 'message' | 'summary'
    role TEXT NOT NULL,            -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    token_count INTEGER,
    source_chunk_id INTEGER,      -- ссылка на chunks.id из mila.db
    created_at TIMESTAMP
);
```

Общая таблица дневника:

```sql
CREATE TABLE diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_number INTEGER,
    content_md TEXT NOT NULL,
    created_at TIMESTAMP,
    UNIQUE(entry_number)
);
```

## 3. FAISS индекс

**Файл**: `data/faiss.index`

Бинарный файл FAISS `IndexIDMap(IndexFlatL2)`. Размерность 768 (по умолчанию).

- `id` в FAISS соответствует `faiss_id` в таблице `memories`
- При удалении воспоминания: soft delete (`is_active=0`) + `rebuild_from_active()` для перестроения индекса

## Связи между таблицами

```
chunks.faiss_id ──→ FAISS index id
chunks.id ──→ context_buffer.chat_{id}.source_chunk_id
memories.faiss_id ──→ FAISS index id
users.id ──→ chunks.user_id ──→ memories.user_id
chats.id ──→ chunks.chat_id
```

## Инициализация

В `main.py`:

```python
from core.db import init_db
init_db()  # создаёт все таблицы, если их нет
```

В `core/context_buffer.py` таблицы создаются лениво при первом обращении к чату (`ensure_chat_table`).

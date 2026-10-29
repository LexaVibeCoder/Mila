# Базы данных

Mila использует два SQLite-файла и FAISS-индекс.

## 1. `mila.db` (основная БД)

**Файл:** `data/mila.db` (`SQLITE_PATH`).

Управляется `core/db.py` (`init_db()`), плюс ленивые таблицы:
`presence_state` (`core/presence.py`), `mila_memory_chunks`
(`core/memory/eternal.py`), таблицы сна (`core/sleep/state.py`),
`user_attitude_history` (`Mila_observatory/api/users.py`).

> Все идентификаторы — **TEXT (UUID4)**, не INTEGER. `init_db()` идемпотентен;
> миграции — ad-hoc `ALTER TABLE`.

`get_conn()` включает:

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA foreign_keys=ON;
```

`timeout=30` в `sqlite3.connect` — bot и Observatory не падают на
«database is locked».

### users

```sql
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    user_name TEXT,
    preferred_name TEXT,
    relationship_type TEXT DEFAULT 'friend',
    sentiment REAL DEFAULT 0.0,
    appearance_description TEXT
);
```

`id` — это `user_id`; полей `is_bot/language_code/created_at/updated_at` нет.

### chats

```sql
CREATE TABLE chats (
    chat_id TEXT PRIMARY KEY,
    chat_name TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### chat_members

```sql
CREATE TABLE chat_members (
    chat_id TEXT,
    user_id TEXT,
    role TEXT DEFAULT 'user',
    PRIMARY KEY (chat_id, user_id),
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
```

### chunks (все сообщения)

```sql
CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,      -- uuid4
    chat_id TEXT NOT NULL,
    user_id TEXT,
    content TEXT,                   -- "" у молчаливых ответов; плейсхолдер у медиа
    llm_full_response TEXT,         -- полный thoughts/ответ LLM
    type TEXT DEFAULT 'raw',        -- всегда 'raw'
    faiss_id INTEGER,               -- обычно NULL
    token_count INTEGER,            -- обычно 0
    time_start TEXT,                -- UTC isoformat
    time_end TEXT,                  -- не заполняется
    kind TEXT DEFAULT 'text',       -- text | photo | voice | ... (миграция)
    media_path TEXT                 -- путь к файлу в data/media
);
```

`kind`/`media_path` добавляются миграцией `_migrate_multimedia()`.

### memories

```sql
CREATE TABLE memories (
    mem_id TEXT PRIMARY KEY,        -- uuid4
    user_id TEXT NOT NULL,
    faiss_id INTEGER,
    type TEXT DEFAULT 'fact',
    tag TEXT,                       -- JSON (поле tag, не tags)
    content TEXT,
    importance REAL DEFAULT 0.5,
    confidence_source TEXT DEFAULT 'llm_extraction',
    confidence_verified INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    time TEXT DEFAULT (datetime('now')),
    source_chat_id TEXT,
    source_message_id TEXT,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
```

Поиск по памяти всегда фильтруется по `user_id` (см. [memory.md](memory.md)).

### media_logs

```sql
CREATE TABLE media_logs (
    media_id TEXT PRIMARY KEY,
    chat_id TEXT,
    user_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    file_path TEXT,
    vlm_description TEXT,
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
```

Заполняется при скачивании фото/войса; `vlm_description` — плейсхолдер текста
(отдельного captioner-а нет, описание даёт main LLM нативно).

### presence_state

```sql
CREATE TABLE presence_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    state TEXT NOT NULL DEFAULT 'rest',  -- rest | dialog | working | sleep
    focus_chat_id TEXT,
    focus_user_name TEXT,
    note TEXT DEFAULT '',
    updated_at TEXT
);
```

Одна строка `id=1`, создаётся лениво. Сон как цикл принадлежит `sleep_state`;
здесь только намерение/локация.

### user_attitude_history

Создаётся лениво при первом обращении к истории отношения или первом изменении:

```sql
CREATE TABLE user_attitude_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    sentiment REAL DEFAULT 0.0,
    relationship_type TEXT DEFAULT 'friend',
    changed_by TEXT DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now'))
);
```

Пишется, когда `PUT /api/users/{id}` меняет `sentiment` или
`relationship_type`; используется графиком отношения.

### mila_memory_chunks (автобиографическая память)

Создаётся лениво `core/memory/eternal.py::_ensure_table()`:

```sql
CREATE TABLE mila_memory_chunks (
    chunk_id TEXT PRIMARY KEY,      -- uuid4
    title TEXT NOT NULL,
    content_md TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    version INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    importance REAL DEFAULT 0.5
);
```

### Таблицы сна

Создаются `core/sleep/state.py::_ensure_sleep_tables()`:

```sql
CREATE TABLE sleep_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    state TEXT NOT NULL DEFAULT 'awake',  -- awake|sleeping|committing|sleep_error
    session_id TEXT,
    current_stage TEXT DEFAULT 'idle',
    progress_pct REAL DEFAULT 0.0,
    started_at TEXT,
    scheduled_end_at TEXT,
    completed_at TEXT,
    triggered_by TEXT DEFAULT 'manual',
    error_message TEXT,
    version INTEGER DEFAULT 1
);

CREATE TABLE sleep_message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    received_at TEXT DEFAULT (datetime('now')),
    processed INTEGER DEFAULT 0
);

CREATE TABLE sleep_transaction_staging (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,  -- insert|update|delete
    target_table TEXT NOT NULL,    -- mila_memory_chunks | memories | context_buffer.chat_...
    target_id TEXT,
    payload TEXT NOT NULL,         -- JSON с данными операции
    created_at TEXT DEFAULT (datetime('now')),
    applied INTEGER DEFAULT 0
);

CREATE TABLE sleep_dialog_summary_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    summary_text TEXT,
    messages_removed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

## 2. `context_buffer.db` (буфер контекста)

**Файл:** `data/context_buffer.db` (`CONTEXT_DB_PATH`).

Управляется `core/context/buffer.py` и `core/diary/manager.py`.

### Таблицы чатов

Для каждого чата — отдельная таблица (имя санитизируется):

```sql
CREATE TABLE [chat_{chat_id}] (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('message', 'summary')),
    source_chunk_id TEXT,       -- chunks.chunk_id (TEXT) или NULL
    token_count INTEGER DEFAULT 0,
    role TEXT NOT NULL DEFAULT '',  -- 'user' | 'assistant' | ''
    kind TEXT DEFAULT 'text',
    created_at TEXT DEFAULT (datetime('now'))  -- локальное время
);
```

### diary_entries

```sql
CREATE TABLE diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_number INTEGER NOT NULL,
    content_md TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'day_entries',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

## 3. FAISS индекс

**Файл:** `data/faiss_text.index` (`FAISS_TEXT_PATH`; `faiss_image.index`
зарезервирован, не используется).

- бинарный `IndexIDMap(IndexFlatL2)`;
- размерность `EMBED_DIM` (по умолчанию **384**, не 768);
- `id` FAISS = `memories.faiss_id`;
- `save()` пишет только при `ntotal > 0`, атомарно (`.tmp` + `os.replace`) под
  межпроцессным `flock`;
- повреждённый индекс при старте переименовывается в `.corrupt-<ts>`;
- автосейв каждые `FAISS_SAVE_INTERVAL_MIN` минут и при `bot.stop()`;
- перестроение — `POST /api/memory/rebuild-faiss` (переэмбеддинг всех
  `memories.content`).

## Связи

```
memories.faiss_id              ──→ FAISS index id
chunks.chunk_id (TEXT UUID)     ──→ context_buffer.chat_*.source_chunk_id
chunks.media_path               ──→ файл в data/media
memories.user_id                ──→ users.user_id
chunks.chat_id                  ──→ chats.chat_id
chunks.user_id                  ──→ users.user_id
user_attitude_history.user_id   ──→ users.user_id
sleep_message_queue.session_id  ──→ sleep_state.session_id
sleep_transaction_staging.session_id ──→ sleep_state.session_id
```

У чанков ключ `chunk_id` (TEXT); `chunks.faiss_id` обычно NULL — эмбеддинги
строятся только для `memories`.

## Инициализация

```python
from core.db import init_db
init_db()  # users/chats/chat_members/chunks/memories/media_logs
```

Остальное создаётся лениво:

- таблицы чатов — `ensure_chat_table()`;
- `diary_entries` — `_ensure_diary_table()`;
- `mila_memory_chunks` — `_ensure_table()`;
- `presence_state` — `_ensure_table()` при первом обращении;
- сон — `_ensure_sleep_tables()`;
- `user_attitude_history` — при первом изменении отношения / запросе истории.

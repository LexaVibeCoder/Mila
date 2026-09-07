# Базы данных

Mila использует два SQLite-файла и FAISS-индекс.

## 1. mila.db (основная БД)

**Файл**: `data/mila.db` (путь — `SQLITE_PATH` в config.py)

Управляется модулем `core/db.py` (`init_db()`), плюс ленивые таблицы сна (`core/sleep/state.py`) и вечной памяти (`core/memory/eternal.py`).

> Важно: все идентификаторы — **TEXT (UUID4)**, не INTEGER. Ниже — актуальные схемы из кода.

### Таблицы

#### users
```sql
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    user_name TEXT,
    preferred_name TEXT,       -- как Мила обращается
    relationship_type TEXT DEFAULT 'friend',
    sentiment REAL DEFAULT 0.0,
    appearance_description TEXT -- (поле называется appearance_description, не appearance)
);
```
Нет полей `is_bot / language_code / created_at / updated_at`, `id` — это `user_id` (TEXT).

#### chats
```sql
CREATE TABLE chats (
    chat_id TEXT PRIMARY KEY,
    chat_name TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```
Поля `type/title/username` нет — только `chat_name` (пишется из Telegram через `tg_adapter`).

#### chat_members
```sql
CREATE TABLE chat_members (
    chat_id TEXT,
    user_id TEXT,
    role TEXT DEFAULT 'user',   -- (поле role есть, joined_at нет)
    PRIMARY KEY (chat_id, user_id)
);
```

#### chunks (все сообщения)
```sql
CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,  -- uuid4
    chat_id TEXT NOT NULL,
    user_id TEXT,
    content TEXT,               -- пусто ("") у молчаливых ответов; плейсхолдер ([фото], [голосовое сообщение]) у медиа
    llm_full_response TEXT,     -- полный thoughts/ответ LLM
    type TEXT DEFAULT 'raw',    -- всегда 'raw' в текущем коде
    faiss_id INTEGER,           -- обычно NULL (эмбеддинги чанков не строятся)
    token_count INTEGER,        -- всегда 0 (подсчёт не ведётся)
    time_start TEXT,            -- UTC isoformat создания
    time_end TEXT,              -- не заполняется
    kind TEXT DEFAULT 'text',   -- text | photo | voice | video | sticker | ... (миграция в init_db)
    media_path TEXT             -- локальный путь скачанного файла (photo/voice) или NULL
);
```

#### memories (долгосрочные воспоминания)
```sql
CREATE TABLE memories (
    mem_id TEXT PRIMARY KEY,    -- uuid4
    user_id TEXT NOT NULL,
    faiss_id INTEGER,           -- связь с FAISS
    type TEXT DEFAULT 'fact',
    tag TEXT,                   -- JSON (поле tag, не tags)
    content TEXT,
    importance REAL DEFAULT 0.5,
    confidence_source TEXT DEFAULT 'llm_extraction',
    confidence_verified INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    time TEXT DEFAULT (datetime('now')),
    source_chat_id TEXT,
    source_message_id TEXT
);
```
Нет `is_active / access_count / created_at / updated_at / confidence / source` — это старые имена.

#### media_logs
```sql
CREATE TABLE media_logs (
    media_id TEXT PRIMARY KEY,
    chat_id TEXT,
    user_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    file_path TEXT,
    vlm_description TEXT
);
```
Заполняется при каждом скачанном фото/войсе (`log_media()` из `queue.handle()`); `vlm_description` — плейсхолдер текста (отдельного капшенера в v1 нет, описание даёт main LLM нативно).

#### presence_state (состояния Милы)
```sql
CREATE TABLE presence_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    state TEXT NOT NULL DEFAULT 'rest',  -- rest | dialog | working | sleep
    focus_chat_id TEXT,                  -- чат диалога (только dialog)
    focus_user_name TEXT,                -- имя собеседника для промпта
    note TEXT DEFAULT '',
    updated_at TEXT
);
```
Одна строка `id=1`, создаётся лениво в `core/presence.py`. Сон как цикл принадлежит `sleep_state`/`sleep manager` — здесь только намерение.
```
Полей `media_type / file_id / caption / created_at` нет; таблица создана, но кодом бота пока не пишется (задел под фото/VLM).

#### mila_memory_chunks (автобиографическая память)
Создаётся лениво `core/memory/eternal.py::_ensure_table()`:
```sql
CREATE TABLE mila_memory_chunks (
    chunk_id TEXT PRIMARY KEY,  -- uuid4
    title TEXT NOT NULL,
    content_md TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    version INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    importance REAL DEFAULT 0.5
);
```

#### Таблицы сна (все в mila.db, создаются в `core/sleep/state.py`)

```sql
-- Одна строка (id=1), состояния: awake|sleeping|committing|sleep_error (строчные!)
CREATE TABLE sleep_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    state TEXT NOT NULL DEFAULT 'awake',
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

-- Очередь сообщений во сне (есть session_id + user_id + received_at/processed)
CREATE TABLE sleep_message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    received_at TEXT DEFAULT (datetime('now')),
    processed INTEGER DEFAULT 0
);

-- Стейдж изменений (универсальный payload-JSON, не old_data/new_data)
CREATE TABLE sleep_transaction_staging (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,  -- insert|update|delete по разным target_table
    target_table TEXT NOT NULL,    -- mila_memory_chunks | memories | diary | context_buffer ...
    target_id TEXT,
    payload TEXT NOT NULL,         -- JSON с данными операции
    created_at TEXT DEFAULT (datetime('now')),
    applied INTEGER DEFAULT 0
);

-- Лог суммаризаций
CREATE TABLE sleep_dialog_summary_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    summary_text TEXT,
    messages_removed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

## 2. context_buffer.db (буфер контекста)

**Файл**: `data/context_buffer.db` (`CONTEXT_DB_PATH`)

Управляется `core/context/buffer.py` (таблицы чатов) + `core/diary/manager.py` (дневник).

### Таблицы

Для каждого чата создаётся отдельная таблица (имя санитизируется):

```sql
CREATE TABLE [chat_{chat_id}] (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('message', 'summary')),
    source_chunk_id TEXT,       -- chunks.chunk_id (TEXT) или NULL
    token_count INTEGER DEFAULT 0,
    role TEXT NOT NULL DEFAULT '',  -- 'user' | 'assistant' | '' (старые summary)
    created_at TEXT DEFAULT (datetime('now'))  -- локальное время
);
```

Общая таблица дневника (с категориями):

```sql
CREATE TABLE diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_number INTEGER NOT NULL,
    content_md TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'day_entries',  -- day_entries | reflection_entries
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

## 3. FAISS индекс

**Файл**: `data/faiss_text.index` (`FAISS_TEXT_PATH`; зарезервирован `data/faiss_image.index`, не используется)

Бинарный файл FAISS `IndexIDMap(IndexFlatL2)`. Размерность `EMBED_DIM` (по умолчанию **384**, не 768).

- `id` в FAISS соответствует `faiss_id` в таблице `memories`
- Старого пути `data/faiss.index` в коде нет
- `save()` пишет файл только при `ntotal > 0`; автосейв каждые `FAISS_SAVE_INTERVAL_MIN` минут
- При удалении воспоминания: soft delete + `rebuild_from_active()` (через Observatory `POST /api/memory/rebuild-faiss` с переэмбеддингом)

## Связи между таблицами

```
memories.faiss_id ──→ FAISS index id
chunks.chunk_id (TEXT UUID) ──→ context_buffer.chat_*.source_chunk_id
memories.user_id ──→ users.user_id
chunks.chat_id ──→ chats.chat_id
chunks.user_id ──→ users.user_id
sleep_message_queue.session_id ──→ sleep_state.session_id
sleep_transaction_staging.session_id ──→ sleep_state.session_id
```

Связи `chunks.faiss_id → FAISS` и `chunks.id → source_chunk_id` из старых версий документации неверны: у чанков ключ `chunk_id` (TEXT), `faiss_id` обычно NULL.

## Инициализация

В `main.py`:

```python
from core.db import init_db
init_db()  # создаёт users/chats/chat_members/chunks/memories/media_logs, если их нет
```

Остальное создаётся лениво: таблицы чатов — `ensure_chat_table()` при первом обращении, `mila_memory_chunks` — при первом вызове eternal-памяти, сон — `_ensure_sleep_tables()`, дневник — `_ensure_diary_table()`.

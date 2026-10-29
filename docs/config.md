# Система конфигурации

Mila использует **два слоя конфигурации**:

1. **Статичный** — `config.py` (pydantic-settings), читается из `.env`.
2. **Динамический** — `RuntimeConfig` (`Mila_observatory/runtime.py`), хранится в `./runtime_config.json`, редактируется через API без перезапуска.

Приоритет:

```
runtime_config.json (значение не null)  →  побеждает  →  config.py (.env)
```

`null` в runtime означает «используй значение из `.env`».

## 1. Статичная конфигурация (`config.py`)

Класс `Settings(BaseSettings)`, `env_file=".env"`, `extra="allow"` (лишние
переменные не ломают старт и доступны runtime-слою).

### Telegram / TDLib

| Поле | По умолчанию | Описание |
|------|--------------|----------|
| `API_ID` | обязателен | `my.telegram.org/apps` |
| `API_HASH` | обязателен | |
| `PHONE_NUMBER` | `""` | Подсказка для auth; можно не указывать |
| `DEVICE_MODEL` | `"PC 64bit"` | |
| `SYSTEM_VERSION` | `"Windows 11"` | |
| `APP_VERSION` | `"5.0.1"` | |
| `TD_FILES_DIRECTORY` | `"data/tdlib"` | База TDLib + скачанные файлы |
| `TD_DATABASE_ENCRYPTION_KEY` | `"mila-tdlib-key-change-me"` | Смена ключа делает существующую сессию нечитаемой |
| `TD_VERBOSITY` | `2` | |

### LLM (день)

| Поле | По умолчанию |
|------|--------------|
| `LLM_API_BASE` | обязателен |
| `LLM_API_KEY` | обязателен |
| `NIGHT_LLM_API_KEY` | пусто = `LLM_API_KEY` |
| `MAIN_LLM_MODEL` | обязателен |
| `MAIN_LLM_MAX_TOKENS` | `2048` |
| `MAIN_LLM_TEMPERATURE` | `0.7` |

### Эмбеддинги

| Поле | По умолчанию |
|------|--------------|
| `EMBED_API_BASE` | обязателен |
| `EMBED_API_KEY` | обязателен (может быть пустым для Ollama) |
| `EMBED_MODEL` | обязателен |
| `EMBED_DIM` | `384` (должна совпадать с моделью) |

### FAISS / память / реранкер

| Поле | По умолчанию |
|------|--------------|
| `FAISS_TEXT_PATH` | `data/faiss_text.index` |
| `FAISS_IMAGE_PATH` | `data/faiss_image.index` (резерв, не используется) |
| `FAISS_SAVE_INTERVAL_MIN` | `10` |
| `RERANKER_MODEL_PATH` | `models/rerankers/BGE-Reranker-v2-m3` |
| `RERANKER_THRESHOLD` | `0.6` |
| `DECAY_LAMBDA` | `0.01` (сидирует runtime) |
| `FORGET_THRESHOLD` | `0.3` (сидирует runtime) |

### Данные и контекст

| Поле | По умолчанию | Описание |
|------|--------------|----------|
| `SQLITE_PATH` | `data/mila.db` | |
| `PHOTOS_DIR` | `data/photos` | Legacy-каталог |
| `MEDIA_DIR` | `data/media` | Персистентные фото/войсы для истории |
| `MAX_MEDIA_MB` | `20` | Лимит скачивания одного файла |
| `DAILY_REPORTS` | `data/daily reports` | **Пробел в пути — квотировать** |
| `MAX_CONTEXT_TOKENS` | `10000` | Сидирует `pipeline.max_context_tokens` |
| `MAX_STM_MESSAGES` | `30` | Лимит `get_context()` по умолчанию |
| `SIMILARITY_THRESHOLD_IMAGE` | `0.85` | Legacy |
| `MAIN_LLM_THOUGHT_SENTENCES` | `3` | Legacy |
| `LOG_DIR` | `logs` | Pipeline- и diary-логи |
| `CONTEXT_DB_PATH` | `data/context_buffer.db` | |
| `CONTEXT_MAX_TOKENS` | `30000` | Лимит токенов сообщений (без summary) |
| `CONTEXT_SUMMARIZE_CHUNK_TOKENS` | `5000` | Сколько сжимать за раз |
| `CONTEXT_MAX_MESSAGES` | `40` | Legacy |
| `CONTEXT_SUMMARIZE_CHUNK` | `20` | Legacy |
| `LOCAL_TIMEZONE` | `""` | IANA-зона; пусто = системная |

### Observatory

| Поле | По умолчанию | Описание |
|------|--------------|----------|
| `OBSERVATORY_USERNAME` | `admin` | Basic Auth |
| `OBSERVATORY_PASSWORD` | `""` | Пусто = auth выключен |
| `OBSERVATORY_HOST` | `""` | Пусто = `0.0.0.0` |
| `OBSERVATORY_PORT` | не поле `Settings` | Порт берётся из `runtime system.observatory_port`; переменная может лежать в `.env` благодаря `extra="allow"`, но код её не читает |

### Сообщения / typing

| Поле | По умолчанию | Описание |
|------|--------------|----------|
| `MESSAGE_DEBOUNCE_SECONDS` | `2.0` | Legacy: wake-on-every игнорирует окно тишины |
| `ENABLE_TYPING_WAIT` | `true` | Мастер-выключатель typing-логики |
| `TYPING_WAIT_MAX_SECONDS` | `8.0` | Верхняя граница ожидания печати |
| `TYPING_FRESHNESS_SECONDS` | `6.0` | Сколько typing-событие считается свежим |
| `TYPING_POLL_INTERVAL` | `0.5` | Шаг опроса |
| `TYPING_GRACE_SECONDS` | `2.0` | Ожидание сообщения после конца печати |
| `TYPING_ABORT_GENERATION` | `true` | Отменять генерацию/отправку при печати |

### Presence: авто-выход из диалога

| Поле | По умолчанию | Описание |
|------|--------------|----------|
| `DIALOG_IDLE_TIMEOUT_SECONDS` | `300.0` | 5 минут тишины в focus-чате → rest |
| `DIALOG_IDLE_CHECK_INTERVAL_SECONDS` | `30.0` | Период проверки watcher-а |

### Локальные модели (задел)

`CLIP_MODEL_PATH`, `MOONDREAM_MODEL_PATH` — в текущем коде не используются.

### Использование

```python
from config import settings
settings.LLM_API_BASE
```

`normalize_api_base(url)` достраивает `/v1`, если его нет (LM Studio);
OpenRouter с `/v1` не меняется.

## 2. Динамическая конфигурация (`RuntimeConfig`)

**Файл:** `Mila_observatory/runtime.py`
**Хранилище:** `./runtime_config.json` в корне проекта (не в `data/`), gitignored.

При старте `RuntimeConfig` загружает JSON, затем `_seed_from_settings()` заполняет
оставшиеся `None` из `.env`. `night_llm.model/api_base` наследуются от
`llm.*`; `night_llm.temperature/max_tokens` по умолчанию `0.3/4096`.

API-ключи в runtime config **не хранятся**: `llm.api_key` и `night_llm.api_key`
всегда берутся из `.env` (`LLM_API_KEY`, `NIGHT_LLM_API_KEY` с fallback на
`LLM_API_KEY`), значения из старого `runtime_config.json` игнорируются и
вырезаются из памяти при загрузке; запись через `set()/update()` невозможна.

### Актуальная структура

```json
{
  "llm": {
    "model": "...",
    "api_base": "https://openrouter.ai/api/v1",
    "temperature": 0.8,
    "max_tokens": 2048,
    "top_p": null,
    "frequency_penalty": null,
    "presence_penalty": null,
    "tool_choice": "auto"
  },
  "night_llm": {
    "model": "...",
    "api_base": "...",
    "temperature": 0.3,
    "max_tokens": 4096,
    "tool_choice": "required"
  },
  "tools": {
    "search_memory": true,
    "send_message": true,
    "silence": true,
    "write_diary": true,
    "update_diary": true,
    "presence": true,
    "peek_recent": true
  },
  "diary": { "save_processed": false },
  "persona": {
    "custom_text": "",
    "parameters": {
      "empathy": 0.8,
      "humor": 0.6,
      "formality": 0.2,
      "creativity": 0.7
    }
  },
  "pipeline": {
    "max_context_tokens": 10000,
    "max_think_iterations": 7,
    "decay_lambda": 0.01,
    "forget_threshold": 0.3,
    "similarity_threshold": 0.85
  },
  "sleep": {
    "start_hour": null,
    "end_hour": null,
    "forced_awake": false
  },
  "proactive": {
    "enabled": true,
    "idle_minutes": 27,
    "check_interval_seconds": 60,
    "sleep_minutes": 480,
    "cooldown_minutes": 27,
    "max_chats": 20,
    "default_peek_limit": 10,
    "command": {
      "run_now": false,
      "dry_run": false,
      "force_action": null,
      "needs_override": {},
      "requested_at": null
    },
    "last_run": null
  },
  "system": {
    "observatory_port": 8080,
    "log_level": "INFO"
  },
  "_meta": {
    "version": 1,
    "last_updated": "...",
    "updated_by": "observatory"
  }
}
```

### Важные замечания

- **`llm.tool_choice`**: пайплайн всегда использует `auto`; `required` из runtime
  принудительно переопределяется (direct-tools схема). Значение хранится и
  отображается в Observatory, но не меняет поведение main-пайплайна.
- **`night_llm.tool_choice`** реально используется ночными процессами.
- **`pipeline.max_think_iterations`** читается на каждом запуске (по умолчанию 7);
  константа `MAX_THINK_ITERATIONS = 7` — fallback.
- **`persona.parameters.*`** хранятся и редактируются, но в system prompt
  **не инжектятся**. Текст персоны собирается из `core/llm/prompts/*/*.md`.
- **`proactive.command`** — одноразовая команда от Observatory к bot-процессу;
  bot сбрасывает `run_now`, выполняет и пишет `proactive.last_run`.
- **`sleep.start_hour/end_hour = null`** — автосон выключен; `forced_awake = true`
  запрещает и ручной сон.
- **`diary.save_processed`** больше не управляет резервным копированием: дневник
  всегда сохраняется в `logs/diary_logs/` перед очисткой.

### Синхронизация между контейнерами

`bot` и `observatory` — разные процессы со своими in-memory копиями. Мост — JSON-файл:

- каждый `get()`/`get_all()`/`set()`/`update()` вызывает `_maybe_auto_reload()` и
  перечитывает файл при изменении `mtime`;
- каждая запись сначала перечитывает файл, чтобы не затереть чужие изменения;
- `get_all()` содержит секреты, для API используется `get_public_copy()` с маскировкой.

### API для работы с конфигом

| Метод | Описание |
|-------|----------|
| `runtime_config.get(key)` | Dot-notation: `"llm.model"`, `"tools.search_memory"` |
| `runtime_config.set(key, value)` | Установить значение |
| `runtime_config.update(updates, prefix)` | Глубокий merge |
| `runtime_config.get_all()` | Полный конфиг (с секретами) |
| `runtime_config.get_public_copy()` | Копия с маскированными ключами |
| `runtime_config.reload()` | Перезагрузить с диска |

### Эндпоинты API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/config` | Полный runtime config (секреты маскированы) |
| PUT | `/api/config` | Merge; значения-маски с `...` игнорируются |
| POST | `/api/config/reload` | Перезагрузка с диска |
| GET | `/api/runtime-state` | Config + путь/наличие файла в `_live` |

### Маскировка секретов

`Mila_observatory/auth.py`:

- `mask_secret("sk-or-v1-abcdef...wxyz")` → `sk-or-v1...wxyz`;
- `should_keep_existing(value)` — `True`, если в строке есть `...`;
- `strip_masked_secrets(updates)` выкидывает такие значения перед записью;
- `mask_secrets_inplace()` оставлен как защита на случай старых данных, но
  API-ключи в runtime config больше не хранятся (только `.env`).

## Файл `.env`

```ini
API_ID=12345
API_HASH=abc123
PHONE_NUMBER=+79001234567

LLM_API_BASE=http://127.0.0.1:1234
LLM_API_KEY=sk-...
MAIN_LLM_MODEL=llama3
# опционально: отдельный ключ для ночного LLM (иначе LLM_API_KEY)
# NIGHT_LLM_API_KEY=

EMBED_API_BASE=http://127.0.0.1:11434
EMBED_API_KEY=
EMBED_MODEL=nomic-embed-text

# Observatory (пусто = auth выключен, слушать только localhost)
OBSERVATORY_USERNAME=admin
OBSERVATORY_PASSWORD=
# OBSERVATORY_HOST=0.0.0.0

# Presence
DIALOG_IDLE_TIMEOUT_SECONDS=300
DIALOG_IDLE_CHECK_INTERVAL_SECONDS=30
```

Полный список — `.env.example`. `NIGHT_LLM_API_KEY` — единственная рабочая
переменная из `NIGHT_LLM_*` (ключ ночного LLM, fallback на `LLM_API_KEY`);
остальные `NIGHT_LLM_*` в `.env.example` носят справочный характер: рабочие
ночные настройки живут в `runtime_config.json` (`night_llm.*`).

## Где применяется runtime config

| Модуль | Что читает |
|--------|------------|
| `Mila_observatory/llm_manager.py` | `llm.*`, `night_llm.*` |
| `core/pipeline.py` | `llm.tool_choice` (переопределяет в auto), `pipeline.max_think_iterations` |
| `core/tools/executor.py` | `tools.*`, `proactive.default_peek_limit/max_chats`, `presence_state` |
| `core/llm/persona.py` | `persona.custom_text` (полностью заменяет статику) |
| `core/proactive.py` | `proactive.*` |
| `core/sleep/manager.py` | `sleep.*` |
| `core/sleep/processes.py` | `night_llm.*` |
| `core/presence.py` | только `config.py` (`DIALOG_IDLE_*`) |
| `Mila_observatory/server.py` | `system.observatory_port` |

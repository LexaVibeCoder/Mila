# Система конфигурации

Mila использует **два слоя конфигурации**:

1. **Статичный** — `config.py` (pydantic-settings), читается из `.env`
2. **Динамический** — `RuntimeConfig` (`Mila_observatory/runtime.py`), хранится в `./runtime_config.json`, редактируется через API без перезапуска

## 1. Статичная конфигурация (config.py)

**Файл**: `config.py`

Класс `Settings(BaseSettings)` читает переменные из `.env`. Точный список полей:

```python
class Settings(BaseSettings):
    # Telegram
    API_ID: int
    API_HASH: str
    PHONE_NUMBER: str = ""
    DEVICE_MODEL: str = "PC 64bit"
    SYSTEM_VERSION: str = "Windows 11"
    APP_VERSION: str = "5.0.1"

    # TDLib (pytdbot)
    TD_FILES_DIRECTORY: str = "data/tdlib"
    TD_DATABASE_ENCRYPTION_KEY: str = "mila-tdlib-key-change-me"
    TD_VERBOSITY: int = 2

    # LLM
    LLM_API_BASE: str
    LLM_API_KEY: str
    MAIN_LLM_MODEL: str
    MAIN_LLM_MAX_TOKENS: int = 2048
    MAIN_LLM_TEMPERATURE: float = 0.7

    # Embeddings
    EMBED_API_BASE: str
    EMBED_API_KEY: str
    EMBED_MODEL: str
    EMBED_DIM: int = 384

    # Local models (задел, пока не используются)
    CLIP_MODEL_PATH: str = "models/clip-vit-base-patch32"
    MOONDREAM_MODEL_PATH: str = "models/moondream-2b"

    # FAISS & Reranker
    FAISS_TEXT_PATH: str = "data/faiss_text.index"
    FAISS_IMAGE_PATH: str = "data/faiss_image.index"
    FAISS_SAVE_INTERVAL_MIN: int = 10
    RERANKER_MODEL_PATH: str = "models/rerankers/BGE-Reranker-v2-m3"
    RERANKER_THRESHOLD: float = 0.6

    # Memory decay (читаются в runtime как pipeline.decay_lambda / forget_threshold)
    DECAY_LAMBDA: float = 0.01
    FORGET_THRESHOLD: float = 0.3

    # Data
    SQLITE_PATH: str = "data/mila.db"
    PHOTOS_DIR: str = "data/photos"
    MEDIA_DIR: str = "data/media"     # скачанные фото/войс для нативного входа в LLM
    MAX_MEDIA_MB: int = 20            # лимит размера одного медиафайла
    DAILY_REPORTS: str = "data/daily reports"

    # Pipeline
    MAX_CONTEXT_TOKENS: int = 10000
    MAX_STM_MESSAGES: int = 30
    SIMILARITY_THRESHOLD_IMAGE: float = 0.85
    MAIN_LLM_THOUGHT_SENTENCES: int = 3
    LOG_DIR: str = "logs"

    # Context buffer
    CONTEXT_DB_PATH: str = "data/context_buffer.db"
    CONTEXT_MAX_TOKENS: int = 30000
    CONTEXT_SUMMARIZE_CHUNK_TOKENS: int = 5000
    CONTEXT_MAX_MESSAGES: int = 40      # legacy, токенный лимит в приоритете
    CONTEXT_SUMMARIZE_CHUNK: int = 20   # legacy

    # Часовой пояс для меток времени (IANA, например "Europe/Moscow").
    # Пусто = системная зона (учитывает TZ / /etc/localtime в Docker).
    LOCAL_TIMEZONE: str = ""

    # Message batching / debounce + typing-aware ожидание
    # (wake-on-every: debounce игнорируется, решение принимает сама Мила)
    MESSAGE_DEBOUNCE_SECONDS: float = 2.0
    ENABLE_TYPING_WAIT: bool = True
    TYPING_WAIT_MAX_SECONDS: float = 8.0
    TYPING_FRESHNESS_SECONDS: float = 6.0
    TYPING_POLL_INTERVAL: float = 0.5
    TYPING_GRACE_SECONDS: float = 2.0
    TYPING_ABORT_GENERATION: bool = True

    class Config:
        env_file = ".env"
        extra = "allow"  # лишние поля .env (night_llm и др.) доступны через runtime
```

Вспомогательная функция `normalize_api_base(url)` достраивает `/v1` в конце `LLM_API_BASE`, если его нет (нужно для LM Studio; для OpenRouter ничего не меняет).

### Использование

```python
from config import settings
settings.LLM_API_BASE
settings.MAIN_LLM_MODEL
```

## 2. Динамическая конфигурация (RuntimeConfig)

**Файл**: `Mila_observatory/runtime.py`

Хранится в `./runtime_config.json` — **в корне проекта, не в `data/`** (чтобы переживать пересоздание `mila_data` volume через bind-mount `.:/app`). Файл gitignored, может содержать секреты.

При старте `RuntimeConfig` сидируется из `config.py` (все `None` заполняются значениями `.env`), затем `night_llm.*` наследуются от `llm.*`, если не заданы.

### Актуальная структура

```json
{
  "llm": {
    "model": "meta/muse-spark-...",
    "api_base": "https://openrouter.ai/api/v1",
    "api_key": "...",
    "temperature": 0.8,
    "max_tokens": 2048,
    "top_p": null,
    "frequency_penalty": null,
    "presence_penalty": null,
    "tool_choice": "auto"
  },
  "night_llm": {
    "model": "...",
    "api_base": "https://openrouter.ai/api/v1",
    "api_key": "...",
    "temperature": 0.3,
    "max_tokens": 4096,
    "tool_choice": "required"
  },
  "tools": {
    "search_memory": true,
    "send_message": true,
    "silence": true,
    "write_diary": true,
    "update_diary": true
  },
  "diary": {
    "save_processed": false
  },
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
    "max_think_iterations": 5,
    "decay_lambda": 0.01,
    "forget_threshold": 0.3,
    "similarity_threshold": 0.85
  },
  "sleep": {
    "start_hour": null,
    "end_hour": null,
    "forced_awake": false
  },
  "system": {
    "observatory_port": 8080,
    "log_level": "INFO"
  },
  "_meta": {
    "version": 1,
    "last_updated": "2026-09-04T22:25:58.622149",
    "updated_by": "observatory"
  }
}
```

Замечания:
- `tool_choice` (`"required"` / `"auto"`) читается пайплайном на каждой итерации (`runtime_config.get("llm.tool_choice", "required")`) и переключается из Observatory (ModelControl).
- Отдельного `night_verifier_llm` **нет** — ночные процессы используют `night_llm` (генерация t=0.3, верификация t=0.1 задаются кодом).
- `persona.parameters.*` (empathy/humor/formality/creativity) хранятся, но в текущий system prompt **не инжектятся** — persona собирается из файлов `core/llm/prompts/*.md` (см. [persona.md](persona.md)).
- `pipeline.max_think_iterations` хранится, но цикл ограничен константой `MAX_THINK_ITERATIONS = 5` в `core/pipeline.py`.
- `sleep.start_hour` / `end_hour` = `null` → автосон выключен; `forced_awake = true` запрещает даже ручной сон.

### Синхронизация между контейнерами

`bot` и `observatory` — отдельные процессы со своими in-memory копиями. Мост — JSON-файл:
- каждый `get()`/`get_all()`/`set()`/`update()` сначала проверяет `mtime` файла и перечитывает его при изменении (`_maybe_auto_reload`);
- каждая запись сначала перечитывает файл, чтобы не затереть чужие изменения.

### API для работы с конфигом

| Метод | Описание |
|-------|----------|
| `runtime_config.get(key)` | Dot-notation: `"llm.model"`, `"tools.search_memory"` |
| `runtime_config.set(key, value)` | Установить значение |
| `runtime_config.update(updates, prefix)` | Глубокий merge |
| `runtime_config.get_all()` | Полный конфиг |
| `runtime_config.reload()` | Перезагрузить с диска |

### Эндпоинты API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/config` | Полный runtime config |
| PUT | `/api/config` | Обновление (merge) |
| POST | `/api/config/reload` | Перезагрузка с диска |
| GET | `/api/runtime-state` | Config + live state |

## Приоритет параметров

```
runtime_config.json (не-null)  →  побеждает  →  config.py (.env)
```

`null` в runtime = «используй значение из `.env`». Маппинг сидирования (`_seed_from_settings`): `llm.model→MAIN_LLM_MODEL`, `llm.api_base→LLM_API_BASE`, `llm.api_key→LLM_API_KEY`, `llm.temperature→MAIN_LLM_TEMPERATURE`, `llm.max_tokens→MAIN_LLM_MAX_TOKENS`, `pipeline.max_context_tokens→MAX_CONTEXT_TOKENS`, `pipeline.decay_lambda→DECAY_LAMBDA`, `pipeline.forget_threshold→FORGET_THRESHOLD`, `pipeline.similarity_threshold→SIMILARITY_THRESHOLD_IMAGE`.

## Файл .env

```ini
API_ID=12345
API_HASH=abc123
PHONE_NUMBER=+79001234567

LLM_API_BASE=http://127.0.0.1:1234
LLM_API_KEY=sk-...
MAIN_LLM_MODEL=llama3

EMBED_API_BASE=http://127.0.0.1:11434
EMBED_API_KEY=
EMBED_MODEL=nomic-embed-text
```

Смотри `.env.example` для полного списка (включая `NIGHT_LLM_*`, `LOCAL_TIMEZONE`, `TD_*`).

## Где применяется runtime config

| Модуль | Что читает |
|--------|------------|
| `Mila_observatory/llm_manager.py` | `llm.*`, `night_llm.*` |
| `core/pipeline.py` | `llm.tool_choice` (на каждой итерации) |
| `core/tools/executor.py` | `tools.*` (enabled/disabled) |
| `core/llm/persona.py` | `persona.custom_text` (полностью заменяет статику) |
| `core/sleep/manager.py` | `sleep.*` (start_hour/end_hour/forced_awake) |
| `core/sleep/processes.py` | `night_llm.*` |
| `Mila_observatory/server.py` | `system.observatory_port` (порт по умолчанию 8080) |

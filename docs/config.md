# Система конфигурации

Mila использует **два слоя конфигурации**:

1. **Статичный** — `config.py` (pydantic-settings), читается из `.env`
2. **Динамический** — `RuntimeConfig` (JSON), редактируется через API без перезапуска

## 1. Статичная конфигурация (config.py)

**Файл**: `config.py`

Класс `Settings(BaseSettings)` читает переменные из `.env`.

```python
class Settings(BaseSettings):
    # Telegram
    API_ID: int
    API_HASH: str
    PHONE_NUMBER: str
    DEVICE_MODEL: str = "PC"
    SYSTEM_VERSION: str = "4.0"

    # LLM
    LLM_API_BASE: str
    LLM_API_KEY: str = ""
    MAIN_LLM_MODEL: str
    MAIN_TEMPERATURE: float = 0.7
    MAIN_MAX_TOKENS: int = 4096

    # Embedding
    EMBED_API_BASE: str
    EMBED_API_KEY: str = ""
    EMBED_MODEL: str = ""
    EMBED_DIM: int = 768

    # Local models
    CLIP_MODEL_PATH: str = ""
    MOONDREAM_MODEL_PATH: str = ""

    # FAISS / Memory
    FAISS_INDEX_PATH: str = "data/faiss.index"
    FAISS_SAVE_INTERVAL_MIN: int = 10
    MEMORY_DECAY_DAYS: int = 30
    MEMORY_IMPORTANCE_THRESHOLD: float = 0.3

    # Reranker
    RERANKER_MODEL_PATH: str = "models/bge-reranker-v2-m3"
    RERANKER_TOP_K: int = 10
    RERANKER_THRESHOLD: float = 0.6

    # Context buffer
    CONTEXT_DB_PATH: str = "data/context_buffer.db"
    CONTEXT_MAX_TOKENS: int = 30000
    CONTEXT_CHUNK_SIZE: int = 5
    CONTEXT_POP_CHUNKS: int = 3

    # Pipeline
    MAX_CONTEXT_TOKENS: int = 30000
    MAX_STM_MESSAGES: int = 50
    THOUGHT_SENTENCES: int = 3
```

### Использование

```python
from config import settings
settings.LLM_API_BASE
settings.MAIN_LLM_MODEL
```

## 2. Динамическая конфигурация (RuntimeConfig)

**Файл**: `Mila_observatory/runtime.py`

Хранится в `data/runtime_config.json`. Может переопределять любой параметр статичной конфигурации.

### Структура по умолчанию

```json
{
  "llm": {
    "model": null,
    "api_base": null,
    "api_key": null,
    "temperature": null,
    "max_tokens": null
  },
  "night_llm": {
    "model": null,
    "api_base": null,
    "api_key": null,
    "temperature": 0.3,
    "max_tokens": 2048
  },
  "night_verifier_llm": {
    "model": null,
    "api_base": null,
    "api_key": null,
    "temperature": 0.1,
    "max_tokens": 1024
  },
  "tools": {
    "search_memory": true,
    "send_message": true,
    "silence": true,
    "write_diary": true
  },
  "diary": {
    "auto_save": true,
    "max_entries_per_day": 10
  },
  "persona": {
    "custom_prompt": null,
    "empathy": 0.7,
    "humor": 0.5,
    "formality": 0.3,
    "creativity": 0.7
  },
  "pipeline": {
    "max_iterations": 5,
    "context_token_limit": 30000
  },
  "sleep": {
    "enabled": true,
    "start_hour": 23,
    "end_hour": 7,
    "timezone": "Asia/Yekaterinburg",
    "duration_minutes": 120
  },
  "system": {
    "observatory_port": 8080,
    "debug_mode": false,
    "log_level": "INFO"
  },
  "_meta": {
    "version": 1,
    "updated_at": null
  }
}
```

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
runtime_config.json  →  переопределяет  →  config.py (.env)
```

Если в runtime_config параметр `null` — используется значение из `.env`.

## Файл .env

```ini
API_ID=12345
API_HASH=abc123
PHONE_NUMBER=+79001234567

LLM_API_BASE=http://localhost:11434/v1
MAIN_LLM_MODEL=llama3

EMBED_API_BASE=http://localhost:11434/v1
EMBED_MODEL=nomic-embed-text
```

Смотри `.env.example` для полного списка.

## Где применяется runtime config

| Модуль | Что читает |
|--------|------------|
| `Mila_observatory/llm_manager.py` | `llm.*`, `night_llm.*`, `night_verifier_llm.*` |
| `core/tools/executor.py` | `tools.*` (enabled/disabled) |
| `core/llm/persona.py` | `persona.*` (custom_prompt, эмпатия, etc.) |
| `core/pipeline.py` | `pipeline.*` |
| `core/sleep/manager.py` | `sleep.*` (расписание, длительность) |
| `core/sleep/processes.py` | `night_llm.*`, `night_verifier_llm.*` |
| `Mila_observatory/server.py` | `system.observatory_port` |

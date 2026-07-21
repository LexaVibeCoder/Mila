# Архитектура проекта

## Общая схема

```
┌─────────────────────────────────────────────────────────────┐
│                        Telegram                             │
└──────────┬─────────────────────────────────────┬────────────┘
           │                                     │
           ▼                                     ▼
┌──────────────────────┐          ┌──────────────────────────┐
│   MilaUserbot (bot)  │          │  Mila Observatory (web)  │
│   core/bot.py         │          │  Mila_observatory/server │
│                      │          │                          │
│  ┌─────────────────┐ │          │  ┌────────────────────┐  │
│  │ ThinkPipeline    │ │          │  │ FastAPI + React    │  │
│  │ core/pipeline.py │ │◄────────►│  │ 9 API routers      │  │
│  └────────┬────────┘ │          │  └────────────────────┘  │
│           │          │          └──────────────────────────┘
│  ┌────────▼────────┐ │
│  │   ToolExecutor   │ │
│  │   core/tools.py  │ │
│  └──┬─────┬─────┬──┘ │
│     │     │     │    │
│     ▼     ▼     ▼    │
│  ┌──┐ ┌───┐ ┌────┐  │
│  │  │ │   │ │    │   │
│  │FAISS│SQLite│Diary│  │
│  └──┘ └───┘ └────┘  │
└──────────────────────┘
```

## Компоненты

### 1. Ядро бота (`core/`)

| Модуль | Назначение |
|--------|------------|
| `bot.py` | `MilaUserbot` — Pyrogram-клиент, хендлер сообщений, сжатие контекста |
| `pipeline.py` | `ThinkPipeline` — цикл рассуждения: LLM → think() → экзекуция тулзов |
| `llm_client.py` | `LLMClient` — OpenAI-совместимая обёртка с реконфигурацией на ходу |
| `tools.py` | `ToolExecutor` — исполнение действий (send_message, search_memory, etc.) |
| `memory.py` | `VectorMemory` — FAISS `IndexFlatL2` + `IndexIDMap` |
| `mila_memory.py` | Автобиографическая память Милы (SQLite + markdown) |
| `context_buffer.py` | Per-chat буфер контекста с токен-аваром |
| `diary.py` | Дневник — записи от имени Милы |
| `persona.py` | Построитель системного промпта |
| `embedding_client.py` | Эмбеддинг-клиент (Ollama / OpenAI) |
| `reranker.py` | BGE-Reranker-v2-m3 для переранжирования |
| `token_counter.py` | Подсчёт токенов через tiktoken |
| `sleep_manager.py` | Оркестратор ночного цикла |
| `sleep_processes.py` | Три параллельных процесса ночи |
| `sleep_state.py` | Машина состояний сна + SQLite |
| `sleep_llm_tools.py` | Тулзы для LLM во сне |
| `db.py` | Основная БД `mila.db` |

### 2. Веб-панель (`Mila_observatory/`)

| Модуль | Назначение |
|--------|------------|
| `server.py` | FastAPI-приложение, CORS, SPA-fallback |
| `runtime.py` | `RuntimeConfig` — динамический конфиг (JSON) |
| `llm_manager.py` | `LLMManager` — управление LLM-клиентами |
| `models.py` | Pydantic-модели API |
| `api/config.py` | CRUD для runtime config |
| `api/dashboard.py` | Статус, события, дашборд |
| `api/diary.py` | Просмотр дневника |
| `api/memory.py` | CRUD памяти + перестроение FAISS |
| `api/model.py` | Настройки модели + тестовый запрос |
| `api/personality.py` | Редактор персоны |
| `api/pipeline.py` | Логи пайплайна |
| `api/sleep.py` | Управление сном |
| `api/tools.py` | Включение/выключение тулзов |

### 3. State Agent (`state_agent/`)

| Модуль | Назначение |
|--------|------------|
| `needs_tracker.py` | Трекер потребностей (ML + rule-based) |
| `train_state_agent.py` | Обучение MLP-модели |
| `api.py` | FastAPI-роутер для веб-панели |
| `infer.py` | CLI-инференс |

## Поток данных: сообщение → ответ

```
1. Telegram → MilaUserbot.handle_text_message()
2. Сохранение в mila.db (chunks)
3. Проверка сна: если спит → в очередь
4. push_message() в context_buffer
5. _compress_context() при превышении лимита токенов
6. _build_histories() — сбор контекста
7. _resolve_roles_in_histories() — определение ролей
8. ThinkPipeline.process_message_stream()
   └── цикл (max 5 итераций):
       ├── LLM.generate(tool_choice="required") → think()
       ├── Парсинг JSON → список actions
       ├── ToolExecutor.execute_action() для каждого
       └── Если search_memory → результаты → повтор
9. Отправка ответа в Telegram
```

## Ключевые паттерны

- **Синглтоны**: `RuntimeConfig`, `LLMManager`, `NeedsTracker` — thread-safe через `threading.Lock`
- **Единый пайплайн**: Одна функция `think()` с `tool_choice="required"`, ретрай валидации
- **Стриминг**: AsyncGenerator событий `thought/action/search_result/done/error`
- **Транзакционный сон**: Все изменения стейджатся → атомарный коммит или откат
- **Двухстадийный LLM**: Генерация (t=0.3) → Верификация (t=0.1)
- **Токен-аварный контекст**: Каждая запись считает токены, сжатие при 30K
- **Двойной эмбеддинг**: Автоопределение Ollama vs OpenAI с fallback

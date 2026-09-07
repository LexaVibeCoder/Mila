# Mila Observatory

**Директория**: `Mila_observatory/`

Веб-панель для мониторинга и управления Милой. FastAPI бэкенд + React SPA фронтенд.

## Архитектура

```
Mila_observatory/
├── server.py            # FastAPI приложение, CORS, SPA-fallback
├── runtime.py           # RuntimeConfig (синглтон, JSON-персистенция в ./runtime_config.json)
├── llm_manager.py       # LLMManager (синглтон)
├── models.py            # Pydantic модели API
├── api/                 # Роутеры
│   ├── config.py        # CRUD runtime config
│   ├── dashboard.py     # Статус, события, дашборд
│   ├── diary.py         # Просмотр дневника (файлы data/daily reports/*.md)
│   ├── memory.py        # CRUD памяти + FAISS rebuild
│   ├── model.py         # Настройки LLM (main/night) + тестовый запрос
│   ├── needs.py         # State Agent роутер потребностей
│   ├── personality.py   # Редактор персоны
│   ├── pipeline.py      # Логи пайплайна + группировка по чатам
│   ├── sleep.py         # Управление сном
│   └── tools.py         # Включение/выключение тулзов
└── frontend/            # React (TypeScript + Vite)
    └── src/
        ├── App.tsx      # 9 роутов
        ├── api/client.ts  # Полный API клиент
        ├── components/Layout.tsx  # Боковая панель
        └── pages/       # 9 страниц (см. ниже)
```

## Запуск

```bash
# Standalone (из корня проекта)
python -m Mila_observatory.server

# Windows-лаунчер (venv + сервер + открытие браузера)
Mila_Observatory.bat

# Через Docker
docker compose up observatory
```

Порт по умолчанию: 8080 (runtime `system.observatory_port`; в compose проброшен `8080:8080`, команда — `uvicorn Mila_observatory.server:app --host 0.0.0.0 --port 8080 --reload`).

## API Эндпоинты

### Dashboard

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/health` | Health check |
| GET | `/api/status` | Системный статус (uptime, кол-во чанков, модель, сон, presence Милы) |
| GET | `/api/events` | Последние события (in-memory, макс 50) |
| GET | `/api/dashboard` | Комбинированные данные дашборда |
| GET | `/api/presence` | Текущее состояние Милы (`state/focus_chat_id/focus_user_name/note/updated_at`) |

### Модель

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/model` | Текущая конфигурация LLM (main + night, ключи маскированы) |
| PUT | `/api/model/main` | Обновление main LLM (model/api_base/api_key/temperature/max_tokens/top_p/.../tool_choice) |
| PUT | `/api/model/night` | Обновление night LLM (те же поля, сохраняется в runtime, подхватывается без рестарта) |
| POST | `/api/model/test` | Тестовый запрос (playground; всегда через main-клиент, `max_retries=1`) |

`tool_choice` (`required/auto`) — runtime-only поле: в `LLMClient` не пробрасывается, читается пайплайном напрямую. Переключатель есть в ModelControl.

### Память

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/memory` | Список/поиск воспоминаний (пагинация, поиск) |
| POST | `/api/memory` | Создание воспоминания + эмбеддинг + FAISS |
| GET | `/api/memory/{mem_id}` | Детали воспоминания |
| PUT | `/api/memory/{mem_id}` | Обновление |
| DELETE | `/api/memory/{mem_id}` | Удаление (soft) |
| POST | `/api/memory/rebuild-faiss` | Перестроение FAISS индекса с переэмбеддингом |

### Тулзы

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/tools` | Список 5 тулзов с состоянием enabled |
| PUT | `/api/tools/{tool_name}` | Включить/выключить тулз (`{enabled: bool}`) |

### Персона

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/persona` | Настройки персоны (`custom_text` + `parameters`) |
| PUT | `/api/persona` | Обновление персоны |
| POST | `/api/persona/reset` | Сброс на defaults |

### Пайплайн

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/pipeline/logs` | Список логов (пагинация `page/limit`, фильтры `success_only/chat_id`; имена чатов резолвятся из `chats/users`) |
| GET | `/api/pipeline/chats` | Чаты с логами: `chat_id/display_name/count/last_timestamp/last_message` (для фильтра в UI) |
| GET | `/api/pipeline/logs/{filename}` | Детали лога (`system_prompt/llm_messages/raw_llm_responses/...`; защита от path traversal) |

### Конфиг

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/config` | Полный runtime config |
| PUT | `/api/config` | Обновление config (merge) |
| POST | `/api/config/reload` | Перезагрузка с диска |
| GET | `/api/runtime-state` | Config + live state |

### Дневник

API читает **файлы** `data/daily reports/*.md` (не SQLite напрямую):

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/diary` | Список дневных отчётов |
| GET | `/api/diary/today` | Сегодняшняя запись |
| GET | `/api/diary/{date}` | Запись по дате (формат ДД.ММ.ГГГГ) |

Отдельной страницы дневника во фронтенде **нет** (только API).

### Сон

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/sleep/start` | Запуск сна |
| POST | `/api/sleep/wake` | Пробуждение (rollback; запрещено в committing) |
| GET | `/api/sleep/status` | Статус сна (state/stage/progress/elapsed/remaining/queue) |
| GET | `/api/sleep/estimate` | Оценка времени |
| GET | `/api/sleep/config` | Конфигурация сна |
| POST | `/api/sleep/config` | Изменение конфигурации |
| GET | `/api/sleep/queue-count` | Очередь сообщений |

### State Agent

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/state-agent` | Статус трекера + текущие потребности |
| POST | `/api/state-agent/run` | Запустить инференс |
| POST | `/api/state-agent/needs` | Переопределить потребности + инференс |

## Фронтенд

React SPA на TypeScript + Vite. В dev-режиме проксирует API на :8080; в проде сервится FastAPI из `frontend/dist/`.

### Страницы (9)

| Путь | Компонент | Описание |
|------|-----------|----------|
| `/` | Dashboard | Статус-карты + события (auto-refresh 5s) |
| `/sleep` | SleepControl | Старт/вейк, прогресс, estimate, `forced_awake`/`save_processed`, очередь |
| `/model` | ModelControl | Формы main/night + тоггл `tool_choice required/auto` + playground |
| `/memory` | MemoryEditor | CRUD таблица, поиск, FAISS rebuild |
| `/personality` | PersonalityEditor | Слайдеры empathy/humor/formality/creativity + `custom_text` |
| `/tools` | ToolsControl | 5 тогглов (включая `update_diary`) |
| `/pipeline` | PipelineViewer | Логи + фильтр по чатам (`/chats`) + модалка деталей |
| `/config` | ConfigPage | Просмотр raw JSON |
| `/state-agent` | StateAgentControl | Метры fatigue/social/boredom, инференс, override-слайдеры |

### Сборка

```bash
cd Mila_observatory/frontend
npm run build    # tsc -b && vite build → dist/
```

Production-сборка копируется в `frontend/dist/` (в Docker — из stage `frontend-builder`) и сервится FastAPI как статика с SPA-fallback (`/*` → `index.html`).

## RuntimeConfig

**Файл**: `Mila_observatory/runtime.py`

Синглтон, хранящий динамический конфиг в памяти + JSON на диске (**`./runtime_config.json` в корне**, не `data/`).

```python
runtime_config = RuntimeConfig()

runtime_config.get("llm.model")          # dot-notation доступ
runtime_config.set("tools.silence", False)
runtime_config.update({"llm": {...}})    # глубокий merge
runtime_config.get_all()                 # полный конфиг
runtime_config.reload()                  # перезагрузка с диска
```

Thread-safe через `threading.RLock`. Bot и Observatory — разные процессы: синхронизация через `mtime` файла (auto-reload при чтении, re-read перед записью). Подробности — [config.md](config.md).

## CORS

В dev-режиме фронтенд на :5173, API на :8080 — CORS настроен на все origins.

## SPA Fallback

```python
@app.get("/{path:path}")
async def spa_fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse("frontend/dist/index.html")
```

Все не-API маршруты отдают `index.html` для client-side routing.

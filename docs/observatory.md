# Mila Observatory

**Директория:** `Mila_observatory/`

Веб-панель для мониторинга и управления Милой. FastAPI-бэкенд + React SPA.

## Архитектура

```
Mila_observatory/
├── server.py            # FastAPI, CORS, Basic Auth, SPA-fallback, health
├── auth.py              # HTTP Basic Auth + маскировка секретов
├── runtime.py           # RuntimeConfig (singleton, ./runtime_config.json)
├── llm_manager.py       # LLMManager (main/night LLM)
├── models.py            # Pydantic-модели API
├── api/
│   ├── config.py        # runtime config
│   ├── dashboard.py     # статус, события, presence, dashboard
│   ├── diary.py         # дневник (daily reports)
│   ├── memory.py        # CRUD памяти + FAISS rebuild
│   ├── model.py         # main/night LLM + playground
│   ├── needs.py         # State Agent + proactive trigger
│   ├── personality.py   # персона
│   ├── pipeline.py      # логи пайплайна + чаты
│   ├── sleep.py         # сон
│   ├── tools.py         # тумблеры тулзов
│   └── users.py         # пользователи, отношение, память, портрет
└── frontend/            # React + TypeScript + Vite
    └── src/
        ├── App.tsx      # 11 маршрутов
        ├── api/client.ts
        ├── components/Layout.tsx
        └── pages/
```

## Запуск

```bash
# Standalone из корня проекта
.venv/bin/python -m Mila_observatory.server

# Windows-лаунчер (venv + сервер + браузер)
Mila_Observatory.bat

# Linux/macOS-лаунчер (сервер в фоне + браузер; --stop, --foreground)
./Mila_Observatory.sh

# Docker
docker compose up -d observatory
```

Хост и порт берутся только из `.env`: `OBSERVATORY_HOST` (по умолчанию
`127.0.0.1`) и `OBSERVATORY_PORT` (по умолчанию `8080`). Runtime config
(`system.observatory_port`) больше не используется. В compose контейнер
слушает `0.0.0.0`, а наружу порт проброшен на `${OBSERVATORY_HOST:-127.0.0.1}`
— для LAN задайте `OBSERVATORY_HOST=0.0.0.0` и включите пароль.

## Авторизация и безопасность

- HTTP Basic Auth, если задан `OBSERVATORY_PASSWORD`; логин —
  `OBSERVATORY_USERNAME` (по умолчанию `admin`).
- `/api/health` публичен; все остальные роутеры и SPA-fallback требуют auth.
- Сравнение учётных данных — `secrets.compare_digest`.
- Если пароль пуст, панель открыта; в консоль печатается предупреждение.
- API не отдаёт и не принимает API-ключи: `llm.api_key`/`night_llm.api_key`
  в runtime config отсутствуют, ключи берутся только из `.env`. Старые
  значения с `...` и любые попытки записи `api_key` игнорируются.
- CORS: не wildcard; разрешён regex
  `https?://(localhost|127.0.0.1)(:\d+)?`, методы
  `GET/POST/PUT/DELETE`, заголовки `Authorization/Content-Type`.

## API Эндпоинты

### Health / Dashboard

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/health` | Публичный health check |
| GET | `/api/status` | Uptime, счётчики, модель, сон, presence |
| GET | `/api/events?limit=` | Последние in-memory события (макс 50) |
| GET | `/api/dashboard` | `status` + последние 10 событий |
| GET | `/api/presence` | `state/focus_chat_id/focus_user_name/note/updated_at` |

### Модель

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/model` | main + night, без `api_key` |
| PUT | `/api/model/main` | model/api_base/temperature/max_tokens/top_p/.../tool_choice/reasoning_effort/response_format/extra_body |
| PUT | `/api/model/night` | те же поля, кроме `response_format` (ночные процессы его не используют); сохраняется в runtime |
| POST | `/api/model/test` | Playground: всегда main-клиент, `max_retries=1` |

`tool_choice` — runtime-only; пайплайн принудительно использует `auto` для
main-пайплайна. Ночные процессы читают `night_llm.tool_choice`.

`response_format` — формат ответа main-пайплайна в диалоге: `json_schema`
(по умолчанию), `json_object` или `prompt` (без нативного формата). Поле
хранится в `llm.response_format` и читается пайплайном в начале каждого хода;
на LLM-клиент не передаётся.

`reasoning_effort` — «think effort» (`none/minimal/low/medium/high/xhigh/max`,
по умолчанию `none`); `extra_body` — JSON-объект провайдерских полей. Оба
применяются к main и night LLM; для OpenRouter effort автоматически уходит в
`extra_body.reasoning.effort`.

### Память

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/memory` | Список/поиск (`query`, `category`, пагинация) |
| POST | `/api/memory` | Создание + эмбеддинг + FAISS |
| GET | `/api/memory/{mem_id}` | Детали |
| PUT | `/api/memory/{mem_id}` | Обновление |
| DELETE | `/api/memory/{mem_id}` | Удаление строки (hard delete) |
| POST | `/api/memory/rebuild-faiss` | Перестроение индекса с переэмбеддингом |

### Пользователи

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/users?search=&sort=` | Список с агрегатами; sort: `last_activity/messages/memories/name` |
| GET | `/api/users/{user_id}` | Профиль, отношение, чаты, память, сообщения |
| PUT | `/api/users/{user_id}` | `user_name`, `preferred_name`, `relationship_type`, `sentiment`, `appearance_description` |
| GET | `/api/users/{user_id}/memories` | Память пользователя (пагинация, поиск) |
| POST | `/api/users/{user_id}/memories` | Добавить память |
| GET | `/api/users/{user_id}/messages` | История `chunks` (пагинация) |
| GET | `/api/users/{user_id}/attitude-history` | История sentiment/relationship |
| POST | `/api/users/{user_id}/portrait` | LLM-портрет по памяти и сообщениям |

`user_attitude_history` создаётся лениво при первом изменении отношения.
`sentiment` ограничен `[-1, 1]`.

### Тулзы

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/tools` | 7 ключей с состояниями |
| PUT | `/api/tools/{tool_name}` | `{enabled: bool}` |

Допустимые имена: `search_memory`, `send_message`, `silence`, `write_diary`,
`update_diary`, `presence`, `peek_recent`.

### Персона

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/persona` | `custom_text`, `parameters`, `using_custom` |
| PUT | `/api/persona` | Обновление |
| POST | `/api/persona/reset` | `custom_text = ""` |

### Пайплайн

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/pipeline/logs` | Пагинация, `success_only`, `chat_id`; display_name из `chats/users` |
| GET | `/api/pipeline/chats` | Чаты с логами для фильтра |
| GET | `/api/pipeline/logs/{filename}` | Детали; защита от path traversal; multimodal → метки |

### Конфиг

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/config` | Публичная копия runtime config |
| PUT | `/api/config` | Merge; маскированные секреты игнорируются |
| POST | `/api/config/reload` | Перечитать `runtime_config.json` |
| GET | `/api/runtime-state` | Config + `_live` (путь/наличие файла) |

### Дневник

Читает файлы `data/daily reports/*.md`:

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/diary` | Список отчётов |
| GET | `/api/diary/today` | Сегодняшний |
| GET | `/api/diary/{date}` | По дате `ДД.ММ.ГГГГ` |

### Сон

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/sleep/start` | `duration_minutes?`, `triggered_by` |
| POST | `/api/sleep/wake` | Пробуждение = rollback; запрещено в `committing` |
| GET | `/api/sleep/status` | state/stage/progress/elapsed/remaining/queue |
| GET | `/api/sleep/estimate` | Оценка по объёму данных |
| GET | `/api/sleep/config` | `start_hour/end_hour/forced_awake/save_processed` |
| POST | `/api/sleep/config` | Обновление конфигурации |
| GET | `/api/sleep/queue-count` | Очередь сообщений |

### State Agent / Proactive

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/state-agent` | Модель, потребности, инференс |
| POST | `/api/state-agent/run` | Принудительный инференс |
| POST | `/api/state-agent/needs` | Override потребностей + инференс |
| POST | `/api/state-agent/trigger` | Одноразовый проактивный запуск (dry-run/force_action) |
| GET | `/api/state-agent/trigger` | Статус команды и `last_run` |

Подробности про trigger — [proactive.md](proactive.md).

## Фронтенд

React SPA на TypeScript + Vite. В dev-режиме проксирует API на хост/порт из
`.env` (`OBSERVATORY_HOST`/`OBSERVATORY_PORT`, по умолчанию `127.0.0.1:8080`);
в проде сервится FastAPI из `frontend/dist/`.

### Маршруты (11)

| Путь | Компонент | Описание |
|------|-----------|----------|
| `/` | Dashboard | Статус, presence, модель, сообщения, события (refresh 5s) |
| `/pipeline` | PipelineViewer | Логи, фильтр по чатам, модалка деталей (включая блок Mila's reasoning), deep-link `?log=` |
| `/users` | UsersList | Список пользователей, поиск, сортировка, sentiment |
| `/users/:user_id` | UserDetail | Отношение, профиль, портрет, память, история, логи |
| `/model` | ModelControl | main/night формы, `tool_choice`, `reasoning_effort`, `response_format`, `extra_body`, playground |
| `/sleep` | SleepControl | Старт/вейк, прогресс, estimate, конфиг, очередь |
| `/memory` | MemoryEditor | CRUD, поиск, FAISS rebuild |
| `/config` | ConfigPage | Runtime State JSON + reload |
| `/tools` | ToolsControl | 7 тумблеров |
| `/personality` | PersonalityEditor | `custom_text` + слайдеры parameters |
| `/state-agent` | StateAgentControl | Потребности, инференс, trigger/dry-run |

### Сборка

```bash
cd Mila_observatory/frontend
npm run build    # tsc -b && vite build → dist/
```

В Docker `dist` собирается в stage `frontend-builder` и копируется в образ; в
dev-режиме можно собирать локально. `dist/` не коммитится.

## SPA Fallback

```python
@app.get("/{full_path:path}", dependencies=[Depends(require_auth)])
async def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    file_path = os.path.join(frontend_dir, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse(os.path.join(frontend_dir, "index.html"))
```

Статика `/assets` и `/icons` монтируется отдельно, если каталоги существуют.

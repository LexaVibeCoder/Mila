# Mila Observatory

**Директория**: `Mila_observatory/`

Веб-панель для мониторинга и управления Милой. FastAPI бэкенд + React SPA фронтенд.

## Архитектура

```
Mila_observatory/
├── server.py            # FastAPI приложение, CORS, SPA-fallback
├── runtime.py           # RuntimeConfig (синглтон, JSON-персистенция)
├── llm_manager.py       # LLMManager (синглтон)
├── models.py            # Pydantic модели API
├── api/                 # Роутеры
│   ├── config.py        # CRUD runtime config
│   ├── dashboard.py     # Статус, события, дашборд
│   ├── diary.py         # Просмотр дневника
│   ├── memory.py        # CRUD памяти + FAISS rebuild
│   ├── model.py         # Настройки LLM + тестовый запрос
│   ├── needs.py         # State Agent роутер потребностей
│   ├── personality.py   # Редактор персоны
│   ├── pipeline.py      # Логи пайплайна
│   ├── sleep.py         # Управление сном
│   └── tools.py         # Включение/выключение тулзов
└── frontend/            # React (TypeScript + Vite)
    └── src/
        ├── App.tsx      # Роуты
        ├── api/client.ts  # Полный API клиент
        ├── components/Layout.tsx  # Боковая панель
        └── pages/       # 9 страниц
```

## Запуск

```bash
# Standalone
python -m Mila_observatory.server

# Через Docker
docker-compose up observatory
```

Порт по умолчанию: 8080 (настраивается в runtime config: `system.observatory_port`).

## API Эндпоинты

### Dashboard

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/health` | Health check |
| GET | `/api/status` | Системный статус (uptime, кол-во чанков, модель, сон) |
| GET | `/api/events` | Последние события (in-memory, макс 50) |
| GET | `/api/dashboard` | Комбинированные данные дашборда |

### Модель

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/model` | Текущая конфигурация LLM |
| PUT | `/api/model/main` | Обновление параметров LLM |
| POST | `/api/model/test` | Тестовый запрос к LLM |

### Память

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/memory` | Список/поиск воспоминаний (пагинация, поиск) |
| POST | `/api/memory` | Создание воспоминания + эмбеддинг + FAISS |
| GET | `/api/memory/{id}` | Детали воспоминания |
| PUT | `/api/memory/{id}` | Обновление |
| DELETE | `/api/memory/{id}` | Удаление (soft) |
| POST | `/api/memory/rebuild-faiss` | Перестроение FAISS индекса |

### Тулзы

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/tools` | Список тулзов с состоянием enabled |
| PUT | `/api/tools/{name}` | Включить/выключить тулз |

### Персона

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/persona` | Настройки персоны |
| PUT | `/api/persona` | Обновление персоны |
| POST | `/api/persona/reset` | Сброс на defaults |

### Пайплайн

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/pipeline/logs` | Список логов (пагинация, фильтр) |
| GET | `/api/pipeline/logs/{filename}` | Детали лога |

### Конфиг

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/config` | Полный runtime config |
| PUT | `/api/config` | Обновление config |
| POST | `/api/config/reload` | Перезагрузка с диска |
| GET | `/api/runtime-state` | Config + live state |

### Дневник

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/diary` | Список дневных отчётов |
| GET | `/api/diary/today` | Сегодняшняя запись |
| GET | `/api/diary/{date}` | Запись по дате (формат ДД.ММ.ГГГГ) |

### Сон

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/sleep/start` | Запуск сна |
| POST | `/api/sleep/wake` | Пробуждение |
| GET | `/api/sleep/status` | Статус сна |
| GET | `/api/sleep/estimate` | Оценка времени |
| GET | `/api/sleep/config` | Конфигурация сна |
| POST | `/api/sleep/config` | Изменение конфигурации |
| GET | `/api/sleep/queue-count` | Очередь сообщений |

## Фронтенд

React SPA на TypeScript + Vite. Проксирует API на :8080 в dev-режиме.

### Страницы

| Путь | Компонент | Описание |
|------|-----------|----------|
| `/` | Dashboard | Статус-карты + события (auto-refresh 5s) |
| `/sleep` | SleepControl | Управление сном |
| `/model` | ModelControl | Редактор LLM + тестовый запрос |
| `/memory` | MemoryEditor | CRUD таблица, поиск, FAISS rebuild |
| `/personality` | PersonalityEditor | Слайдеры эмпатии/юмора/формальности/креативности + текст |
| `/tools` | ToolsControl | Переключатели тулзов |
| `/pipeline` | PipelineViewer | Логи с модальным окном деталей |
| `/config` | ConfigPage | Просмотр raw JSON |
| `/state-agent` | StateAgentControl | Метры потребностей, инференс, слайдеры |

### Сборка

```bash
cd Mila_observatory/frontend
npm run build    # tsc -b && vite build → dist/
```

Production-сборка копируется в `frontend/dist/` и сервится FastAPI как статика с SPA-fallback (`/*` → `index.html`).

## RuntimeConfig

**Файл**: `Mila_observatory/runtime.py`

Синглтон, хранящий динамический конфиг в памяти + JSON на диске (`data/runtime_config.json`).

```python
runtime_config = RuntimeConfig()

runtime_config.get("llm.model")          # dot-notation доступ
runtime_config.set("tools.silence", False)
runtime_config.update({"llm": {...}})    # глубокий merge
runtime_config.get_all()                 # полный конфиг
runtime_config.reload()                  # перезагрузка с диска
```

Thread-safe через `threading.Lock`.

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

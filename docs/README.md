# Mila

**Mila** — Telegram-юзербот с «живой» персоной: она общается от имени обычного аккаунта, помнит собеседников, ведёт дневник, «спит» и осмысляет прошедший день, а иногда пишет первой.

Под капотом: TDLib (pytdbot) + любая OpenAI-совместимая LLM + векторная память FAISS. Управлять и наблюдать за ботом можно через веб-панель **Observatory** (FastAPI + React).

> Документация описывает **текущее состояние кода**. Если текст и код расходятся — прав код.

---

## Возможности

| Что умеет | Как это работает |
|-----------|------------------|
| Диалог с LLM | Пайплайн рассуждения, тулзы, JSON-ответы, имитация typing и presence |
| Долгая память | FAISS + SQLite, изоляция по пользователям, поиск с reranker |
| Дневник | Записи по категориям, подмешиваются в промпт |
| Ночная рефлексия | Транзакционный цикл с откатом и восстановлением после сбоев |
| Проактивность | State Agent оценивает «потребности» и решает, когда написать первой |
| Observatory | Веб-панель: состояние, API, правка настроек на лету |

---

## Что понадобится

- Python **3.11** (или Docker)
- Аккаунт Telegram и `API_ID` / `API_HASH` (получить на [my.telegram.org](https://my.telegram.org))
- Доступ к LLM с OpenAI-совместимым API и к модели эмбеддингов
- Node.js — только если собираете или разрабатываете фронтенд вручную

> Юзербот действует от имени реального аккаунта. Используйте отдельный аккаунт и учитывайте правила Telegram.

---

## Быстрый старт

### Вариант 1. Docker (проще всего)

```bash
cp .env.example .env          # заполните API_ID, API_HASH, LLM_*, EMBED_*
docker compose up -d --build  # поднимет сервисы bot и observatory
```

Перед первым запуском нужно один раз авторизоваться в Telegram (см. шаг 3 ниже).

### Вариант 2. Локально

Самый простой путь — один установщик. Он идемпотентен: создаёт `.venv` и ставит
зависимости, создаёт `.env` (и ждёт, пока вы заполните обязательные поля),
проводит вход в Telegram и спрашивает, скачивать ли reranker. На первом входе
также можно выбрать, шифровать ли базу TDLib (по умолчанию — шифровать; флаги
`--encrypt` / `--no-encrypt`). Всё уже готовое пропускает; бота не запускает.

```bash
./start/install.sh            # Windows: start\install.bat
```

Ручной вариант тех же шагов:

```bash
# 1. Окружение
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt

# 2. Конфигурация
cp .env.example .env          # заполните API_ID/API_HASH, LLM_*, EMBED_*

# 3. Первый вход в Telegram (один раз; код из Telegram или QR)
.venv/bin/python create_session.py
#   или: ./start/create_session.sh --qr

# 4. Reranker для поиска по памяти (без него search_memory не работает)
.venv/bin/python install_reranker.py

# 5. Запуск бота
.venv/bin/python main.py

# 6. Веб-панель (отдельный процесс, http://localhost:8080)
.venv/bin/python -m Mila_observatory.server
```

**Windows:** установщик — `start\install.bat`, сессия — `start/create_session.bat`, панель с открытием браузера — `Mila_Observatory.bat`.

---

## Как это устроено

```
Telegram ──► TDLib-адаптер ──► очередь ──► ThinkPipeline ──► ответ
                                               │
                    ┌──────────────┬───────────┼──────────────┐
                 Память         Дневник    Персона/промпты   Тулзы
                (FAISS+SQLite)                               

State Agent ──► проактивные решения ──► пайплайн
Ночная рефлексия ──► память и дневник
Observatory ◄──► runtime_config.json ◄──► бот
```

Бот и Observatory — **два отдельных процесса**. Они обмениваются настройками через `runtime_config.json`, поэтому большинство параметров можно менять без перезапуска.

---

## Документация

**С чего начать:** [Архитектура](architecture-overview.md) → [Конфигурация](config.md) → [Docker](docker.md).

| Раздел | О чём |
|--------|-------|
| [Архитектура](architecture-overview.md) | Компоненты, потоки данных, ключевые паттерны |
| [Конфигурация](config.md) | Два слоя: `.env` (`config.py`) и `runtime_config.json` |
| [Docker](docker.md) | Compose, healthcheck, volumes, пересборка фронтенда |
| [Сообщения](messaging.md) | Уведомления, presence, typing, медиа, TDLib-адаптер и auth |
| [Основной пайплайн](pipeline.md) | `ThinkPipeline`: direct-tools, JSON-ответы, поток событий |
| [Тулзы](tools.md) | Состав тулзов по состояниям, executor, `peek_recent_chats` |
| [LLM-клиент](llm-client.md) | OpenAI-совместимая обёртка, ретраи, `response_format`, маскировка ключей |
| [Персона](persona.md) | System prompt из `core/llm/prompts/*/*.md`, состояние presence |
| [Память](memory.md) | FAISS + SQLite `memories`, автобиографическая память, изоляция по пользователю |
| [Контекстный буфер](context-buffer.md) | Per-chat SQLite-буфер, аварийное сжатие по токенам, повторное подключение медиа |
| [Дневник](diary.md) | `diary_entries`, категории, daily-файлы, интеграция с промптом |
| [Ночная рефлексия](sleep-system.md) | 3 процесса, staging, commit/rollback, recovery |
| [State Agent](state-agent.md) | Потребности (MLP + rule-based), офсеты, проактивные решения |
| [Проактивность](proactive.md) | От State Agent до инициативы; `runtime_config` как мост bot ↔ Observatory |
| [Observatory](observatory.md) | FastAPI + React, API, Basic Auth, runtime config |
| [Базы данных](database.md) | Схема `mila.db`, `context_buffer.db`, FAISS |

---

## Структура репозитория

```
main.py, config.py       точка входа и статичные настройки из .env
core/                    бот, очередь, пайплайн, память, сон, потребности
core/llm/prompts/        персона и контекстные блоки (.md)
Mila_observatory/        FastAPI-сервер и React-панель
state_agent/             обучение и инференс MLP
tests/                   unit-тесты
docs/                    документация
```

---

## Разработка

```bash
# Тесты и проверка синтаксиса
.venv/bin/python -m pytest                        # unit-тесты (integration исключены)
.venv/bin/python -m compileall -q core Mila_observatory main.py config.py

# Observatory отдельно
.venv/bin/python -m Mila_observatory.server

# Фронтенд
cd Mila_observatory/frontend
npm ci
npm run dev                                       # Vite на :5173, proxy /api → :8080
npm run build                                     # tsc -b + vite build → dist/

# State Agent
cd state_agent
python train_state_agent.py                       # обучение MLP → state_agent.pt
python infer.py --seconds 3600 --hour 14 --fatigue 0.3 \
  --social 0.7 --boredom 0.6 --dialog 1           # CLI-инференс

# Ручной E2E-тест (отправляет реальные сообщения!)
.venv/bin/python test_tg_mila_dialogue.py
```
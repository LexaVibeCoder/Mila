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

- Python **3.11** (или Docker Engine 20.10+ с Compose 2.x)
- Аккаунт Telegram и `API_ID` / `API_HASH` (получить на [my.telegram.org](https://my.telegram.org))
- Доступ к LLM с OpenAI-совместимым API и к модели эмбеддингов
  (размерность должна совпадать с `EMBED_DIM`; по умолчанию 384)
- Node.js 20+ — для локальной панели (сборка `frontend/dist`) или разработки фронтенда; в Docker не нужен
- Несколько ГБ свободного места (torch, эмбеддинги, reranker ~2 ГБ)

> Юзербот действует от имени реального аккаунта. Используйте отдельный аккаунт и учитывайте правила Telegram.

---

## Быстрый старт

Сначала склонируйте репозиторий и перейдите в него:

```bash
git clone https://github.com/LexaVibeCoder/Mila.git
cd Mila
```

### Вариант 1. Docker (проще всего)

При первом запуске скрипт создаст `.env` и сгенерирует
`TD_DATABASE_ENCRYPTION_KEY`, затем выйдет:

```bash
./start/docker.sh             # создаст .env и попросит заполнить значения
# заполните API_ID, API_HASH, LLM_*, EMBED_* (и EMBED_DIM, если нужна не 384)
./start/docker.sh             # проверки + запуск/переиспользование контейнеров
```

Скрипт сам проверит TDLib-сессию и reranker, предложит войти в Telegram и
скачать модель, если их нет. Существующие контейнеры не пересоздаются.

Если `.env` уже существует, убедитесь, что в нём задан уникальный
`TD_DATABASE_ENCRYPTION_KEY`: без него TDLib использует известный дефолтный
ключ. Менять ключ у уже созданной сессии нельзя — база станет нечитаемой.

Вручную:

```bash
docker compose up -d --build
./start/docker.sh --login     # или --login --qr
```

Панель — `http://127.0.0.1:8080` (порт/интерфейс из `.env`).

> **Windows:** `start/docker.sh` — bash-скрипт. Запускайте его через WSL или
> Git Bash либо используйте `docker compose up -d --build` напрямую.

### Вариант 2. Локально

Самый простой путь — один установщик. Он идемпотентен: создаёт `.venv` и ставит
зависимости, создаёт `.env` (и ждёт, пока вы заполните обязательные поля),
проводит вход в Telegram, спрашивает, скачивать ли reranker, и собирает
фронтенд панели, если найден `npm`. На первом входе также можно выбрать,
шифровать ли базу TDLib (по умолчанию — шифровать; флаги `--encrypt` /
`--no-encrypt`). Всё уже готовое пропускает; бота не запускает.

```bash
./start/install.sh            # Windows: start\install.bat
```

После установки запустите бота и панель в двух терминалах:

```bash
.venv/bin/python main.py
.venv/bin/python -m Mila_observatory.server
```

Если `npm` не был найден, установщик предупредит: панель запустится без UI.
Соберите фронтенд вручную (см. шаг 5 ниже) или используйте Docker.

Ручной вариант тех же шагов:

```bash
# 1. Окружение
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt

# 2. Конфигурация
cp .env.example .env          # заполните API_ID/API_HASH, LLM_*, EMBED_*
# EMBED_DIM должен совпадать с моделью: 384 для nomic-embed-text,
# 1536 для text-embedding-3-small и т.п.
# Для новой TDLib-сессии задайте уникальный ключ:
.venv/bin/python start/set_env.py TD_DATABASE_ENCRYPTION_KEY --random

# 3. Первый вход в Telegram (один раз; код из Telegram или QR)
.venv/bin/python create_session.py
#   или: ./start/create_session.sh --qr

# 4. Reranker для переранжирования памяти (опционален: без него поиск
#    работает по порядку FAISS, но качество выдачи ниже)
.venv/bin/python install_reranker.py

# 5. Сборка фронтенда для локальной панели (нужен Node.js 20+)
cd Mila_observatory/frontend
npm ci
npm run build
cd ../..

# 6. Запуск бота
.venv/bin/python main.py

# 7. Веб-панель (отдельный процесс, http://127.0.0.1:8080 — порт из OBSERVATORY_PORT)
.venv/bin/python -m Mila_observatory.server
```

**Windows:** установщик — `start\install.bat`, сессия — `start/create_session.bat`, панель с открытием браузера — `Mila_Observatory.bat`. Установщик создаёт `.venv` и собирает фронтенд; `Mila_Observatory.bat` использует это окружение. Запуск бота вручную: `.venv\Scripts\python.exe main.py`, панели: `.venv\Scripts\python.exe -m Mila_observatory.server`.

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
| [Docker](docker.md) | Compose, `start/docker.sh`, healthcheck, volumes, пересборка фронтенда |
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
start/                   установщик, вход в Telegram, Docker-лаунчер
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
npm run dev                                       # Vite на :5173, proxy /api → OBSERVATORY_PUBLISH_HOST:OBSERVATORY_PORT (или OBSERVATORY_HOST)
npm run build                                     # tsc -b + vite build → dist/
cd ../..

# State Agent (отдельное окружение: scikit-learn/tqdm не входят в корневой requirements.txt)
cd state_agent
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python train_state_agent.py             # обучение MLP → state_agent.pt
.venv/bin/python infer.py --seconds 3600 --hour 14 --fatigue 0.3 \
  --social 0.7 --boredom 0.6 --dialog 1           # CLI-инференс
cd ..
#   или из корня: ./state_agent/train_and_test.sh

# Ручной E2E-тест (отправляет реальные сообщения!)
.venv/bin/python test_tg_mila_dialogue.py
```
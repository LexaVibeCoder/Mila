# AGENTS.md — Mila

Краткий навигатор для ИИ-агента. Подробная документация — в [`docs/`](docs/README.md);
этот файл задаёт **правила работы**, а не дублирует архитектуру.

## Что это

Mila — Telegram-**юзербот** (не бот-аккаунт) на pytdbot/TDLib. Отвечает в личных
сообщениях от имени живого аккаунта, «думает» через LLM (прямые tool calls
`enter_dialog`/`send_message`/...),
помнит через FAISS + SQLite, ведёт дневник, ночью рефлексирует, имеет потребности
(State Agent) и веб-панель Observatory (FastAPI + React).

Главный поток: Telegram → `core/bot.py` → `core/queue.py` → `core/pipeline.py`
(LLM ↔ `core/tools/executor.py`) → отправка. Детали — `docs/architecture-overview.md`.

## Стек и версии

- Python **3.11** — целевая версия (Dockerfile, скрипты). На хосте может быть новее;
  для воспроизведения создавай `.venv` на 3.11.
- `Pytdbot[tdjson]==0.10.1` и `tdjson==1.8.65` жёстко парные — не обновляй по отдельности.
- LLM/embeddings — любой OpenAI-совместимый API; FAISS `IndexFlatL2`; SQLite (WAL).
- Frontend: React 18 + TS + Vite (`Mila_observatory/frontend`), сервер отдаёт `dist/`.

## Команды

```bash
# окружение (uv умеет поставить Python 3.11 сам; pip-путь — если 3.11 уже есть)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt

# запуск
.venv/bin/python main.py                      # бот; РЕАЛЬНЫЕ сообщения — только с разрешения
.venv/bin/python -m Mila_observatory.server   # панель на OBSERVATORY_HOST:OBSERVATORY_PORT (по умолчанию 127.0.0.1:8080)
cd Mila_observatory/frontend && npm ci && npm run dev   # Vite :5173, proxy /api → тот же хост/порт
cd Mila_observatory/frontend && npm run build           # tsc -b + vite build → dist/

# проверки
.venv/bin/python -m pytest                    # unit-тесты, integration исключены
.venv/bin/python -m compileall -q core Mila_observatory main.py config.py
docker compose up -d --build                  # локальная разработка, не прод

# разово
./start/install.sh                            # полная установка: .venv, deps, .env, сессия (+выбор шифрования), reranker
./start/create_session.sh --qr                # TDLib-логин (или python create_session.py)
python install_reranker.py                    # скачать BGE-reranker в models/
cd state_agent && python train_state_agent.py # обучение MLP (свой requirements.txt)
```

## Карта репозитория

| Путь | Что внутри |
|------|-----------|
| `main.py`, `config.py` | точка входа; статичный `Settings` из `.env` |
| `core/bot.py`, `queue.py`, `tg_adapter.py`, `events.py` | TDLib-клиент, per-chat воркеры, нормализация сообщений |
| `core/pipeline.py`, `tools/executor.py` | direct-tools цикл, 9 действий (+ legacy `silence`), JSON-ответы в диалоге |
| `core/llm/` | клиент, эмбеддинги, reranker, токены, сборка персоны |
| `core/llm/prompts/` | **контент персоны** (`.md`), код только склеивает |
| `core/memory/`, `context/`, `diary/` | FAISS + `memories`, per-chat буфер, дневник |
| `core/presence.py`, `proactive.py`, `needs/` | rest/dialog/working/sleep, проактивные тики, потребности |
| `core/sleep/` | транзакционный ночной цикл (staging → commit/rollback) |
| `core/db.py` | схема `mila.db`, TEXT-UUID идентификаторы |
| `Mila_observatory/` | FastAPI, runtime-конфиг, API-роутеры, React-панель |
| `state_agent/` | обучение/инференс MLP 6→128→64→4 + `state_agent.pt` |
| `test_tg_mila_dialogue.py` | ручной E2E-диалог через Telegram |
| `docs/` | актуальное описание подсистем |

## Конфигурация (два слоя)

1. `.env` → `config.py` (`Settings`), читается при старте.
2. `runtime_config.json` (корень, gitignored) → `Mila_observatory/runtime.py`;
   **перекрывает `.env`**, синхронизируется между процессами по mtime.

Следствие: если поведение не меняется после правки `.env`, причина может быть в
runtime-конфиге. Агенту **нельзя читать `runtime_config.json`** — попроси
пользователя проверить/изменить его через Observatory (`GET/PUT /api/config`).

## Секреты и данные — жёсткие правила

- **`.env` нельзя читать, менять, печатать, грепать, коммитить.** Нужен секрет —
  попроси пользователя выставить переменную окружения. Смотреть можно только `.env.example`.
- **`runtime_config.json` нельзя читать** (может содержать ключи); менять — только
  через пользователя/API.
- **`data/tdlib/` — живая TDLib-сессия. Не трогать, не копировать, не удалять,
  не запускать второй процесс на этом каталоге.** То же для `data/tdlib_test/`.
  Смена `TD_DATABASE_ENCRYPTION_KEY` делает сессию нечитаемой.
- `data/` в остальном читать/писать можно (тесты — только во временных каталогах).
  `models/` читать можно. **Ничего из `data/`, `models/`, `logs/`, `.env`,
  `runtime_config.json`, `dist/`, `node_modules/`, `state_agent/*.pt` не коммитить.**
- Не логировать ключи/токены и содержимое `.env`, даже фрагментами.

## Тесты

Тестов почти нет; при изменении логики добавляй unit-тесты, а не полагайся на
ручной прогон.

- `tests/` повторяет структуру `core/`: `tests/test_<module>.py`; запуск `pytest`.
- `tests/conftest.py` **до импорта `config`** подставляет dummy-env и временные пути.
  Любой тест обязан работать без `.env`, без сети, без Telegram и без реальной `data/`.
- Пути БД захватываются в модулях при импорте (`core.db.DB_PATH`,
  `core.context.buffer.CONTEXT_DB_PATH`) — в тестах переопределяй их через
  `monkeypatch`, БД создавай в `tmp_path`.
- Сеть/LLM/TDLib — только `@pytest.mark.integration` (по умолчанию исключены).
- Async-тесты — `pytest-asyncio` (`asyncio_mode = auto`).
- Frontend: обязательный гейт — `npm run build` (typecheck + сборка). Для чистой
  логики (форматирование, API-клиент) можно добавить Vitest; браузерные E2E — только
  по просьбе. `dist/` не коммитить.
- `test_tg_mila_dialogue.py` — интерактивный E2E, не часть `pytest`; запускать только
  с разрешения (шлёт реальные сообщения, отдельная сессия `data/tdlib_test`).

## Инварианты и подводные камни

- **Живой запуск `main.py` — только после явного разрешения.** Перед этим проверь,
  что на `data/tdlib` не работает другой процесс (Docker/старый бот).
- **Два процесса пишут в одну SQLite** (бот + Observatory) — WAL и `busy_timeout`
  уже настроены; не добавляй долгие транзакции и не блокируй БД.
- Схема БД создаётся идемпотентно (`CREATE TABLE IF NOT EXISTS`, ad-hoc `ALTER`).
  Миграций нет — изменения схемы должны быть безопасны при повторном старте.
- Идентификаторы `users/chats/chunks/memories` — **TEXT UUID**, не INTEGER.
- Персона живёт в `core/llm/prompts/*.md`. Не хардкодь текст персоны в Python и не
  меняй промпты «попутно»: это поведение продукта, а не рефакторинг.
- Сон транзакционный (`sleep_transaction_staging`): не удаляй staging и не меняй
  состояния (`awake/sleeping/committing/sleep_error`) без понимания recovery.
- `tool_choice`, включённые тулзы, лимиты и модели читаются из runtime на ходу;
  не кэшируй их навсегда.
- Долгоживущие asyncio-задачи (`queue`, `proactive`, `sleep`) ломаются тихо —
  проверяй отмены, таймауты и повторный вход.
- Не блокируй event loop: синхронный SQLite/файловый I/O — короткими вызовами.
- Путь `data/daily reports` содержит пробел — всегда квотируй.
- Docker монтирует `.:/app` и volume на `/app/data`: файлы из репозитория `data/`
  внутри контейнера не видны; поэтому `runtime_config.json` лежит в корне.
- Целевая платформа — Linux/Docker, но есть Windows-обёртки; не тащи Unix-only
  вызовы в общий код без fallback.

## Стиль кода

- Следуй окружающему стилю: английские идентификаторы, русский текст промптов/UI,
  комментарии объясняют «почему», а не «что». Не делай массовый реформат.
- В `core/` исторически `print()`, в `main.py` — `logging`. Не переводи весь проект
  на новый логгер без просьбы.
- Новые тяжёлые зависимости — только с согласия. Не меняй пины pytdbot/tdjson.
- Публичные функции/модули — с коротким docstring; сложные места — с комментарием.

## Git и документация

- **Не коммить без просьбы, никогда не пуш.** Ветка `main`.
- Стиль сообщений — Conventional Commits на английском: `feat:`, `fix:`,
  `refactor:`, `docs:`, `chore:` (см. `git log`).
- Перед завершением: `git status`; убедись, что не застейджены секреты/данные.
- При изменении поведения обновляй соответствующий `docs/*.md` **в том же изменении**.
  Документация на русском; код — источник истины при расхождениях.
- Если задача неоднозначна или требует живого запуска/секрета — **спроси**, не угадывай.

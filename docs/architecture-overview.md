# Архитектура проекта

## Компоненты

### 1. Ядро бота (`core/`)

| Модуль | Назначение |
|--------|------------|
| `bot.py` | `MilaUserbot` — pytdbot/TDLib-клиент, фильтр лички, сжатие контекста, точка входа генерации |
| `queue.py` | `MessageCoordinate` — per-chat воркер уведомлений: wake-on-every + typing-aware ожидание/отмена, typing только перед отправкой (см. [messaging.md](messaging.md)) |
| `tg_adapter.py` | Изоляция pytdbot: `NormalizedMessage`, `normalize_event`/`classify_message_content`/`download_notification_media`, resolve/send_text/send_typing/parse actions |
| `events.py` | `Notification` — единый формат входящего (kind/text/caption/file/local_path) + плейсхолдеры |
| `presence.py` | `PresenceManager` — состояния `rest/dialog/working/sleep` в `mila.db`, `format_state_block()` для промпта |
| `media.py` | Нативный vision/audio вход: ресайз фото → `image_url`, войс → `input_audio` |
| `tg_auth.py` | Интерактивный auth TDLib (телефон/код/2FA, предупреждения для Docker без TTY) |
| `pipeline.py` | `ThinkPipeline` — цикл рассуждения: LLM → think() → экзекуция тулзов |
| `llm/client.py` | `LLMClient` — OpenAI-совместимая обёртка с реконфигурацией на ходу |
| `llm/persona.py` | Построитель системного промпта из `llm/prompts/*.md` + runtime-контекст |
| `llm/prompts/` | 23 `.md` фрагмента персоны (статика + шаблоны, см. [persona.md](persona.md)) |
| `llm/embeddings.py` | Эмбеддинг-клиент (Ollama / OpenAI с автоопределением и fallback) |
| `llm/reranker.py` | BGE-Reranker-v2-m3 для переранжирования |
| `llm/counter.py` | Подсчёт токенов через tiktoken (`cl100k_base`) |
| `tools/executor.py` | `ToolExecutor` — 9 действий: send/search/silence/write_diary/update_diary + enter_dialog/enter_rest/enter_working/start_sleep |
| `memory/vector.py` | `VectorMemory` — FAISS `IndexFlatL2` + `IndexIDMap` (`data/faiss_text.index`) |
| `memory/eternal.py` | Автобиографическая память `mila_memory_chunks` (ленивая таблица) |
| `context/buffer.py` | Per-chat буфер контекста с токен-аваром + локальные метки времени |
| `diary/manager.py` | Дневник (`diary_entries` + `data/daily reports/ДД.ММ.ГГГГ.md`, см. [diary.md](diary.md)) |
| `needs/tracker.py` | `NeedsTracker` — потребности + MLP/rule-based инференс (см. [state-agent.md](state-agent.md)) |
| `sleep/manager.py` | Оркестратор ночного цикла (start/wake/commit/rollback/recover/scheduler) |
| `sleep/processes.py` | Три параллельных процесса ночи |
| `sleep/state.py` | Машина состояний сна + SQLite (одна строка `sleep_state id=1`) |
| `sleep/tools.py` | Тулзы для LLM во сне |
| `db.py` | Основная БД `mila.db` (TEXT UUID-идентификаторы) |

### 2. Веб-панель (`Mila_observatory/`)

| Модуль | Назначение |
|--------|------------|
| `server.py` | FastAPI-приложение, CORS, SPA-fallback |
| `runtime.py` | `RuntimeConfig` — динамический конфиг (`./runtime_config.json`) |
| `llm_manager.py` | `LLMManager` — управление LLM-клиентами (main/night, маскировка ключей) |
| `models.py` | Pydantic-модели API |
| `api/config.py` | CRUD для runtime config |
| `api/dashboard.py` | Статус, события, дашборд |
| `api/diary.py` | Просмотр дневника (файлы daily reports) |
| `api/memory.py` | CRUD памяти + перестроение FAISS |
| `api/model.py` | Настройки main/night LLM + тестовый запрос |
| `api/needs.py` | State Agent роутер (`/api/state-agent*`) |
| `api/personality.py` | Редактор персоны |
| `api/pipeline.py` | Логи пайплайна + группировка по чатам |
| `api/sleep.py` | Управление сном |
| `api/tools.py` | Включение/выключение тулзов |
| `frontend/` | React+TS+Vite SPA (9 страниц, см. [observatory.md](observatory.md)) |

### 3. State Agent

| Модуль | Назначение |
|--------|------------|
| `core/needs/tracker.py` | `NeedsTracker` — трекер потребностей (ML + rule-based), офсеты действий, кэш 2с |
| `state_agent/train_state_agent.py` | Обучение MLP 6→128→64→4 |
| `state_agent/infer.py` | CLI-инференс |
| `state_agent/dataset.json` | Тренировочные сэмплы |
| `state_agent/state_agent.pt` | Обученные веса `{state_dict, mean, std, config}` |
| `Mila_observatory/api/needs.py` | FastAPI-роутер для веб-панели |

### 4. Скрипты и запуск (корень)

| Файл | Назначение |
|------|------------|
| `main.py` | Точка входа: `makedirs` + `init_db()` + `MilaUserbot().start()` |
| `create_session.py` | One-shot TDLib-логин (300с ожидание `AuthorizationStateReady`) |
| `start/create_session.sh` / `.bat` | Обёртки: venv + зависимости + `create_session.py` |
| `install_reranker.py` | Скачивание `BAAI/bge-reranker-v2-m3` → `models/rerankers/BGE-Reranker-v2-m3` |
| `test_tg_mila_dialogue.py` | E2E-тест диалога: тестер-LLM ↔ Мила через Telegram, стенограмма + оценка в `logs/` |
| `Mila_Observatory.bat` | Windows-лаунчер Observatory (venv + `server.py` + браузер) |
| `runtime_config.json` | Живой динамический конфиг (gitignored, может содержать секреты) |

## Поток данных: сообщение → ответ

```
1. Telegram (TDLib) → MilaUserbot: все личные сообщения (текст/фото/войс/...),
   свои исходящие пропускаются (bot.py)
2. MessageCoordinate.handle(): normalize_event → ensure_user/chat → download media
   → add_chunk(kind, media_path) + log_media → push placeholder в context_buffer
3. Проверка сна: спит → queue_incoming_message(), конец
4. Воркер чата: wake-on-every (без debounce) → typing-wait
   (ждёт конца печати + grace 2с, макс 8с) → генерация
5. _compress_context() при > 30K токенов сообщений (старые → summary)
6. _build_histories() — контекст в лимите (суммари + новейшие) + presence-блок
   (rest/dialog/working/sleep, имена вместо id)
7. ThinkPipeline.process_message_stream() — цикл до 5 итераций, текущее
   фото/войс — нативно (image_url/input_audio), история — плейсхолдерами:
   ├── LLM.generate(tool_choice из runtime) → think(thoughts, actions)
   ├── Парсинг JSON → список actions
   ├── ToolExecutor.execute_action() + NeedsTracker.apply_action_effect()
   ├── search_memory(searched) или enter_*(switched) → инъекция → повтор;
   │   silence / start_sleep → стоп
8. queue.py: send_message → send_typing + отправка в Telegram (пауза 0.3–1с;
   typing ТОЛЬКО перед отправкой, не во время решения),
   silence/fallback-обработка; новое сообщение/печать во время генерации → отмена и рестарт
9. Ночью: sleep-процессы читают дневник/диалоги → стейджинг → коммит в память
   (сон запускается и тулом start_sleep с duration_minutes)
```

Подробно про шаги 2–4, 8 — [messaging.md](messaging.md); про шаг 7 — [pipeline.md](pipeline.md).

## Ключевые паттерны

- **Синглтоны**: `RuntimeConfig` (`RLock` + mtime-синхронизация между контейнерами), `LLMManager`, `NeedsTracker` (ленивый, без лока)
- **Единый пайплайн**: одна функция `think(thoughts, actions)`; `tool_choice` переключается runtime (`required/auto`); 1 ретрай валидации на первой итерации
- **Стриминг**: AsyncGenerator событий `thought/action/search_result/done/error`
- **Транзакционный сон**: все изменения стейджатся в `sleep_transaction_staging` → атомарный коммит или откат; состояния строчные (`awake/sleeping/committing/sleep_error`)
- **Двухстадийный LLM ночью**: генерация (t=0.3) → верификация (t=0.1) на одном `night_llm`
- **Токен-аварный контекст**: подсчёт при вставке (tiktoken), сжатие чанками по ~5K при 30K; суммари всегда первые
- **Двойной эмбеддинг**: автоопределение Ollama (`:11434`/`ollama` в хосте → `/api/embed`) vs OpenAI-совместимый, взаимный fallback
- **Файловые промпты**: весь текст персоны — в `core/llm/prompts/*.md`, код только склеивает
- **TEXT-идентификаторы**: `users/chats/chunks/memories` на UUID-строках, не INTEGER

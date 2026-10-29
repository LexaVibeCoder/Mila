# Архитектура проекта

## Компоненты

### 1. Ядро бота (`core/`)

| Модуль | Назначение |
|--------|------------|
| `bot.py` | `MilaUserbot` — pytdbot/TDLib-клиент, фильтр личек, сжатие контекста, сборка историй, graceful shutdown |
| `queue.py` | `MessageCoordinate` — per-chat воркер: wake-on-every, typing-aware ожидание/отмена, отправка «пузырями», защита от ответа во сне |
| `tg_adapter.py` | Изоляция pytdbot: `NormalizedMessage`, `normalize_event`, `classify_message_content`, `download_notification_media`, send/resolve/parse helpers |
| `events.py` | `Notification` — единый формат входящего (kind/text/caption/file/local_path) + плейсхолдеры |
| `presence.py` | `PresenceManager` — состояния `rest/dialog/working/sleep`, авто-выход из диалога в rest |
| `media.py` | Нативный vision/audio вход: ресайз фото → `image_url`, ogg/opus → wav → `input_audio`, персистентное хранение |
| `tg_auth.py` | Интерактивный auth TDLib (телефон/код/2FA/email/QR, предупреждения для Docker без TTY) |
| `pipeline.py` | `ThinkPipeline` — direct-tools цикл: LLM → tool calls или JSON-ответ, до 7 итераций |
| `proactive.py` | Проактивный планировщик: State Agent после глобальной тишины, ручной trigger, `peek_recent_chats` |
| `llm/client.py` | `LLMClient` — OpenAI-совместимая обёртка, ретраи, `response_format`, fallback без аудио |
| `llm/persona.py` | Сборка system prompt из `llm/prompts/*/*.md` + presence/needs/дневник |
| `llm/prompts/` | 26 `.md`-фрагментов: persona, relationship, context, diary, states |
| `llm/embeddings.py` | Эмбеддинг-клиент (Ollama native / OpenAI-compatible, взаимный fallback) |
| `llm/reranker.py` | BGE-Reranker-v2-m3 для переранжирования результатов памяти |
| `llm/counter.py` | Подсчёт токенов через tiktoken (`cl100k_base`) |
| `tools/executor.py` | `ToolExecutor` + `get_tool_definitions(presence_state, include_proactive)` |
| `memory/vector.py` | `VectorMemory` — FAISS `IndexFlatL2` + `IndexIDMap`, атомарное сохранение |
| `memory/eternal.py` | Автобиографическая память `mila_memory_chunks` (ленивая таблица) |
| `context/buffer.py` | Per-chat буфер контекста, токен-аварная выборка и сжатие |
| `diary/manager.py` | Дневник (`diary_entries` + `data/daily reports/ДД.ММ.ГГГГ.md`) |
| `needs/tracker.py` | `NeedsTracker` — потребности + MLP/rule-based инференс |
| `sleep/manager.py` | Оркестратор ночного цикла (start/wake/commit/rollback/recover/scheduler) |
| `sleep/processes.py` | Три параллельных процесса ночи |
| `sleep/state.py` | Машина состояний сна + SQLite (одна строка `sleep_state id=1`) |
| `sleep/tools.py` | Тулзы для LLM во сне |
| `db.py` | Основная БД `mila.db` (TEXT UUID-идентификаторы) |

### 2. Веб-панель (`Mila_observatory/`)

| Модуль | Назначение |
|--------|------------|
| `server.py` | FastAPI-приложение, CORS, Basic Auth, SPA-fallback, healthcheck |
| `auth.py` | HTTP Basic Auth, маскировка и защита секретов в API |
| `runtime.py` | `RuntimeConfig` — динамический конфиг (`./runtime_config.json`) |
| `llm_manager.py` | `LLMManager` — main/night LLM, runtime-overrides, playground |
| `models.py` | Pydantic-модели API |
| `api/config.py` | Просмотр/merge/reload runtime config |
| `api/dashboard.py` | Статус, события, presence, дашборд |
| `api/diary.py` | Просмотр дневника (файлы daily reports) |
| `api/memory.py` | CRUD памяти + перестроение FAISS |
| `api/model.py` | Настройки main/night LLM + тестовый запрос |
| `api/needs.py` | State Agent + проактивный trigger (`/api/state-agent*`) |
| `api/personality.py` | Редактор персоны |
| `api/pipeline.py` | Логи пайплайна, группировка по чатам, sanitize мультимодальных сообщений |
| `api/sleep.py` | Управление сном |
| `api/tools.py` | Включение/выключение тулзов |
| `api/users.py` | Пользователи: отношение, память, сообщения, портрет |
| `frontend/` | React+TS+Vite SPA (11 маршрутов, см. [observatory.md](observatory.md)) |

### 3. State Agent

| Модуль | Назначение |
|--------|------------|
| `core/needs/tracker.py` | `NeedsTracker` — трекер потребностей (ML + rule-based), офсеты действий, кэш 2с |
| `state_agent/train_state_agent.py` | Обучение MLP 6→128→64→4 |
| `state_agent/infer.py` | CLI-инференс |
| `state_agent/dataset.json` | Тренировочные сэмплы (локальный файл, gitignored) |
| `state_agent/state_agent.pt` | Обученные веса `{state_dict, mean, std, config}` (gitignored) |
| `Mila_observatory/api/needs.py` | FastAPI-роутер для веб-панели |

### 4. Скрипты и запуск (корень)

| Файл | Назначение |
|------|------------|
| `main.py` | Точка входа: logging + `makedirs` + `init_db()` + `MilaUserbot.start()` + graceful stop |
| `create_session.py` | One-shot TDLib-логин (300с ожидание `AuthorizationStateReady`, `--qr`) |
| `start/create_session.sh` / `.bat` | Обёртки: временный venv + зависимости + `create_session.py` |
| `install_reranker.py` | Скачивание `BAAI/bge-reranker-v2-m3` → `models/rerankers/BGE-Reranker-v2-m3` |
| `test_tg_mila_dialogue.py` | E2E-тест диалога: тестер-LLM ↔ Мила через Telegram, стенограмма + оценка в `logs/` |
| `Mila_Observatory.bat` | Windows-лаунчер Observatory (venv + `server.py` + браузер) |
| `runtime_config.json` | Живой динамический конфиг (gitignored, может содержать секреты) |

## Поток данных: сообщение → ответ

```
1. Telegram (TDLib) → MilaUserbot: только личные чаты; свои исходящие пропускаются.
2. MessageCoordinate.handle(): normalize_event → ensure_user/chat →
   download photo/voice → persist в data/media → add_chunk + log_media →
   push placeholder в context_buffer → накопление текста и медиа батча.
3. Если Мила спит: сообщение всё равно сохраняется в chunks/buffer,
   затем queue_incoming_message() — ответа нет.
4. Воркер чата: wake-on-every (без debounce) → typing-wait
   (ждёт конца печати + grace 2с, максимум 8с) → генерация.
5. _compress_context(): при > 30K токенов сообщений старые сообщения
   сначала суммаризируются, затем удаляются (постфактум, без потери при сбое).
6. _build_histories(): summary + новейшие сообщения в лимите; префиксы времени.
7. ThinkPipeline.process_message_stream():
   ├── system prompt собирается заново на каждой итерации под текущий presence;
   ├── набор тулзов зависит от состояния (rest/dialog/working/proactive);
   ├── LLM.generate(...) с tool_choice="auto";
   ├── dialog: модель возвращает JSON {"messages": [...]} (1–3 пузыря) или [];
   ├── rest/working: только enter_dialog / send_message / смена состояния;
   ├── search_memory / peek_recent_chats / diary → инъекция результата → повтор;
   ├── enter_* → prompt пересобирается, LLM вызывается в новом состоянии;
   └── start_sleep / пустой ответ → завершение без отправки.
8. queue.py: reply-событие → проверки прерывания и сна → send_typing →
   пауза 0.3–1.0с → send_text для каждого пузыря → chunks + context_buffer.
   Текст в rest без enter_dialog игнорируется; fallback-отправки больше нет.
9. Фоновые циклы:
   ├── auto-rest: dialog без активности 5 мин → rest;
   ├── proactive: глобальная тишина 27 мин → State Agent → write_to_user /
   │   sleep / wait / plan_day (см. proactive.md);
   └── sleep: ночной цикл по расписанию, вручную или тулом start_sleep.
10. Ночью: sleep-процессы читают дневник/диалоги/память → staging →
    commit или rollback.
```

Подробно про шаги 2–4 и 8 — [messaging.md](messaging.md); про шаг 7 — [pipeline.md](pipeline.md); про 9 — [proactive.md](proactive.md); про 10 — [sleep-system.md](sleep-system.md).

## Ключевые паттерны

- **Direct tools вместо `think()`-обёртки**: LLM вызывает настоящие функции (`enter_dialog`, `send_message`, `search_memory`, ...). В диалоге ответ — обычный JSON-контент, а не tool call.
- **`tool_choice` всегда `auto`**: runtime-значение `required` принудительно переопределяется в пайплайне; иначе модель не смогла бы просто ответить или промолчать.
- **Состояние определяет инструменты**: `get_tool_definitions(presence_state=...)` отдаёт только релевантный набор; после `enter_*` prompt и tools пересобираются.
- **JSON-ответы в диалоге**: `response_format=json_schema` при поддержке провайдера, иначе prompt + парсер + ретрай. Пустой массив = молчание.
- **Пузыри**: ответ в диалоге — 0–3 коротких сообщения; отправка последовательная, с typing и паузами.
- **Прерывание**: новое сообщение или печать собеседника отменяют генерацию/отправку; недоставленный батч возвращается в очередь.
- **Проактивность через JSON-мост**: Observatory пишет команду в `runtime_config`, bot-процесс подхватывает её в течение ~10с и пишет `last_run`.
- **Транзакционный сон**: изменения стейджатся в `sleep_transaction_staging` → атомарный commit или rollback; состояние переживает рестарт через wall-clock deadline в БД.
- **Двухстадийный LLM ночью**: генерация (t=0.3) → верификация (t=0.1) на одном `night_llm`.
- **Токен-аварный контекст**: подсчёт при вставке (tiktoken), сжатие чанками по ~5K при 30K.
- **Двойной эмбеддинг**: автоопределение Ollama (`:11434`/`ollama` в хосте → `/api/embed`) vs OpenAI-совместимый, взаимный fallback.
- **Файловые промпты**: весь текст персоны — в `core/llm/prompts/*.md`, код только склеивает.
- **TEXT-идентификаторы**: `users/chats/chunks/memories` на UUID-строках, не INTEGER.
- **Секреты не покидают API**: ключи маскируются (`abcd1234...wxyz`), значения с `...` не перезаписываются.

# Ночная рефлексия (Sleep System)

Система ночной рефлексии — фоновый цикл, запускаемый вручную, по расписанию или
тулом `start_sleep`. Три процесса идут параллельно (`asyncio.gather`), каждый
использует двухстадийную LLM-проверку: генерация (t=0.3) → верификация (t=0.1)
на одном `night_llm`.

## Архитектура

```
core/sleep/manager.py   — оркестратор (start/wake/execute/commit/rollback/recover/scheduler)
core/sleep/processes.py — 3 процесса (run_all_processes через asyncio.gather)
core/sleep/state.py     — машина состояний + SQLite (одна строка sleep_state id=1)
core/sleep/tools.py     — определения тулзов для LLM
```

## Машина состояний

Значения **строчные** (`SleepState(str, Enum)`):

```
awake ──→ sleeping ──→ committing ──→ awake
              │              │
              └──→ sleep_error ──→ awake (rollback)
```

Одна строка `sleep_state WHERE id=1` в `mila.db`:
`state / session_id / current_stage / progress_pct / started_at /
scheduled_end_at / completed_at / triggered_by / error_message / version`.

Стадии `SleepStage`: `diary_analysis / dialog_summarization /
user_fact_extraction / verification / committing / completed / idle`.

- ручное пробуждение во время `sleeping` = rollback;
- во время `committing` пробуждение запрещено;
- `scheduled_end_at` — wall-clock дедлайн, переживает рестарт;
- «спящая» фаза после обработки помечается `stage=idle`, `progress≈90`.

## Запуск и длительность

`start_sleep(duration_minutes=None, triggered_by="manual")`:

- отказывает, если уже `sleeping`/`committing` или `sleep.forced_awake=true`;
- создаёт `session_id`, пишет `sleeping` + presence `sleep`, сбрасывает офсеты
  потребностей;
- запускает `_run_sleep_with_timeout`:
  - с длительностью — таймаут `duration*60 + 300с`; по таймауту принудительный
    wake (если не идёт commit);
  - без длительности — сон до ручного пробуждения;
- manual sleep отменяет ожидающую auto-sleep задачу.

## Процессы

### 1. Анализ дневника (`process_diary_analysis`)

- Если дневник пуст — успех без изменений.
- LLM читает дневник + `mila_memory_chunks` + метаданные чанков.
- Тулзы: `add_memory_chunk`, `update_memory_chunk`, `delete_memory_chunk`.
- До `MAX_GENERATION_ITERATIONS = 3` попыток; если модель не вызвала тул —
  system-ошибка и повтор.
- Верификация: `approve_changes` / `reject_changes`, до
  `MAX_VERIFICATION_RETRIES = 3`. При rejection — повторная генерация
  (до 3 раз).

### 2. Суммаризация диалогов (`process_dialog_summarization`)

- Берёт чаты с сообщениями после прошлого сна (`completed_at`).
- Пропускает чат, если сообщений меньше `SUMMARIZE_MIN_MESSAGES = 10` или
  контекст пуст.
- Тулзы: `save_dialog_summary`, `skip_dialog`.
- `save_dialog_summary`:
  - `push_summary(chat_id, summary)`;
  - `pop_oldest_messages_by_tokens(chat_id, 50000)`;
  - `detach_media_by_chunk_ids()` для summarized-away сообщений — очищает
    `chunks.media_path` и удаляет файлы (включая `.wav`), чтобы старые
    фото/аудио не подтягивались в будущие ходы;
  - пишет `sleep_dialog_summary_log`;
  - ставит staging-операцию для `context_buffer.chat_*`.
- Верификация: approve/reject; при rejection результат помечается rejected.

### 3. Извлечение фактов (`process_user_fact_extraction`)

- Берёт последние 200 `chunks` каждого активного чата.
- Тулзы: `add_user_fact`, `skip_user_fact_extraction`.
- `add_user_fact` пишет в `memories` (source_chat_id обязателен) и ставит
  staging-операцию `insert` на `memories`.
- Поддерживает групповые чаты: `user_id` берётся из префикса сообщения.
- Верификация approve/reject; при rejection — до 3 повторных извлечений.

## Стейдж и атомарность

Важный нюанс: операции **применяются к БД сразу** (chunk/memory/diary-summary
создаются в процессе), а `sleep_transaction_staging` хранит запись о том, что
нужно откатить. Это позволяет rollback удалить вставки и реактивировать
удалённые чанки.

Записывается `{session_id, operation_type, target_table, target_id, payload,
applied}`:

- `insert/update/delete` по `mila_memory_chunks`;
- `insert` по `memories`;
- `insert` summary в `context_buffer.chat_*`.

### Commit (`commit_all`)

1. `state = committing`;
2. все staging-операции помечаются `applied=1`;
3. дневник сохраняется в `logs/diary_logs/`; при успехе — очищается;
4. `state = awake`, presence → `rest`.

Сами данные уже лежат в целевых таблицах; commit фиксирует их и убирает
возможность rollback.

### Rollback (`rollback_all`)

1. Идёт по staging в обратном порядке:
   - `insert` chunk → удалить строку;
   - `delete` chunk → `is_active=1`;
   - `insert` memory → удалить `mem_id`;
   - `update` chunk **не откатывается автоматически** (пишется предупреждение);
2. очищает staging сессии;
3. всегда пытается сохранить дневник в `logs/diary_logs/`; очищает таблицу
   только при успешном бэкапе;
4. `state = awake`, presence → `rest`.

## Crash Recovery

`MilaUserbot.start()` → `recover_from_crash()`:

- `awake` → ничего;
- `sleeping`:
  - если стадия `idle` и прогресс ≥ 89 — процессы уже завершены, сон
    «дремлет»; при живом дедлайне — возобновить ожидание
    (`_resume_sleep_until_deadline`), при истёкшем — commit;
  - иначе — `rollback_all()` (сон прервался во время обработки);
- `committing`:
  - staging пуст → всё применено → `awake`;
  - иначе → rollback;
- `sleep_error` → rollback.

Wall-clock дедлайн из БД авторитетнее in-memory таймера: после рестарта сон
продолжается до `scheduled_end_at`, а не засыпает заново.

## Auto-sleep

`start_auto_sleep_scheduler()` — цикл с проверкой каждые 60с:

```python
start_hour = runtime_config.get("sleep.start_hour")
end_hour = runtime_config.get("sleep.end_hour")
forced_awake = runtime_config.get("sleep.forced_awake", False)
```

- `forced_awake = true` → автосон выключен **и** ручной `start_sleep`
  отклоняется;
- `start_hour is None or end_hour is None` → автосон выключен (дефолт);
- диапазон поддерживает переход через полночь;
- при попадании в окно и `state == awake` → `start_sleep(triggered_by="automatic")`
  без длительности (до ручного пробуждения).

Длительность из конфига не читается: она передаётся параметром
`start_sleep(duration_minutes)` / API и влияет только на таймаут.

## Очередь сообщений

Во время сна `queue.py`:

1. сохраняет сообщение в `chunks` и `context_buffer`;
2. вызывает `queue_incoming_message(chat_id, user_id, placeholder)` с текущим
   `session_id`;
3. не запускает генерацию.

Счётчик — `GET /api/sleep/queue-count`. Автоматического replay-консьюмера в
текущем коде **нет**: после пробуждения сообщения уже находятся в `chunks` и
буфере, поэтому следующий входящий ход строит контекст с их учётом. Вспомогательные
функции `take_unprocessed_sleep_queue()` / `mark_messages_processed()` есть, но
hot path их не вызывает.

## Управление через API

| Эндпоинт | Описание |
|----------|----------|
| `POST /api/sleep/start` | Ручной запуск (`duration_minutes?`, `triggered_by`) |
| `POST /api/sleep/wake` | Пробуждение = rollback; запрещено в `committing` |
| `GET /api/sleep/status` | state/stage/progress/elapsed/remaining/queue_count |
| `GET /api/sleep/estimate` | Оценка по числу записей дневника и диалогов |
| `GET /api/sleep/config` | `start_hour/end_hour/forced_awake/save_processed` |
| `POST /api/sleep/config` | Изменение конфигурации |
| `GET /api/sleep/queue-count` | Сообщений в очереди |

## LLM настройки для сна

Единая секция `night_llm` (отдельного `night_verifier_llm` нет):

```json
{
  "night_llm": {
    "model": "...",
    "api_base": "https://openrouter.ai/api/v1",
    "temperature": 0.3,
    "max_tokens": 4096,
    "tool_choice": "required"
  }
}
```

- ключ ночного LLM берётся из `.env`: `NIGHT_LLM_API_KEY`, если задан, иначе
  `LLM_API_KEY`; в runtime config ключа нет;
- незаданные `model/api_base` наследуются от `llm.*` при старте;
- `temperature/max_tokens` по умолчанию `0.3/4096`;
- верификатор — тот же клиент с `temperature=0.1`, `max_tokens=2048`;
- `night_llm.tool_choice` читается на каждом генерационном/верификационном
  вызове; если модель плохо переносит `required`, значение можно переключить
  в Observatory.

# Ночная рефлексия (Sleep System)

Система ночной рефлексии — асинхронный фоновый процесс, запускаемый вручную, по расписанию или до таймаута. Состоит из трёх параллельных процессов, каждый использует двухстадийную LLM-проверку (генерация t=0.3 → верификация t=0.1, обе на `night_llm` из runtime).

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

Состояние — одна строка `sleep_state WHERE id=1` в `mila.db`: `state / session_id / current_stage / progress_pct / started_at / scheduled_end_at / completed_at / triggered_by / error_message`. Стадии `SleepStage`: `diary_analysis / dialog_summarization / user_fact_extraction / verification / committing / completed / idle`. Ручное пробуждение во время `sleeping` = rollback; во время `committing` пробуждение запрещено.

## Процессы (запускаются параллельно через `asyncio.gather`)

### 1. Анализ дневника (`process_diary_analysis`)

**Цель**: LLM читает дневник Милы + её вечную память (`mila_memory_chunks`) и решает, нужно ли добавить/обновить/удалить чанки.

**Тулзы**:
- `add_memory_chunk(title, content_md, importance)` — создать чанк
- `update_memory_chunk(chunk_id, title, content_md, importance)` — обновить
- `delete_memory_chunk(chunk_id)` — мягкое удаление

**Верификация**: второй проход тем же `night_llm` (t=0.1) через `approve_changes` / `reject_changes`. При rejection → ретрай генерации (до 3 попыток).

### 2. Суммаризация диалогов (`process_dialog_summarization`)

**Цель**: для каждого чата с новыми сообщениями с прошлого сна LLM создаёт краткую суммаризацию или пропускает диалог.

**Тулзы**:
- `save_dialog_summary(chat_id, summary)` — сохранить (лог в `sleep_dialog_summary_log`)
- `skip_dialog(chat_id)` — пропустить

После суммаризации старые сообщения удаляются из контекстного буфера.

### 3. Извлечение фактов о пользователях (`process_user_fact_extraction`)

**Цель**: для каждого активного диалога LLM извлекает долгосрочные факты о пользователях.

**Тулзы**:
- `add_user_fact(user_id, fact_text, confidence)` — добавить факт (→ `memories`)
- `skip_user_fact_extraction()` — пропустить

Поддерживает групповые чаты (определение пользователя по `from_user_id`).

## Стейдж и атомарность

Все изменения СНАЧАЛА записываются в `sleep_transaction_staging` как `{session_id, operation_type, target_table, target_id, payload(JSON), applied}`:

- вставки/обновления/удаления чанков `mila_memory_chunks`
- добавленные `memories`
- очистка дневника, записи лога суммаризаций

**Commit** (`commit_all`): staged → applied, дневник сохраняется в `logs/diary_logs/` + `data/daily reports/`, очищается, состояние → `awake`.
**Rollback** (`rollback_all`): обратные операции по `payload` → восстановление исходного состояния.

## Crash Recovery

При старте `MilaUserbot.start()` вызывает `recover_from_crash()`:
1. Читает `sleep_state` (id=1)
2. `awake` → ничего не делать
3. `sleeping` / `sleep_error` → `rollback_all()` → `awake`
4. `committing`: staged пуст → все изменения применены → `awake`; иначе → `rollback_all()`

## Auto-sleep

`start_auto_sleep_scheduler()` запускает `auto_sleep_scheduler()` — фоновый цикл с проверкой каждые 60 секунд (`_check_auto_sleep`):

```python
start_hour = runtime_config.get("sleep.start_hour")  # например, 23 или None
end_hour = runtime_config.get("sleep.end_hour")      # например, 7 или None
forced_awake = runtime_config.get("sleep.forced_awake", False)
```

- `forced_awake = true` → автосон выключен И ручной `start_sleep` отклоняется
- `start_hour is None or end_hour is None` → автосон выключен (значение по умолчанию!)
- иначе диапазон с поддержкой перехода через полночь → `start_sleep(triggered_by="automatic")`, сон без длительности (до ручного пробуждения/таймаута)
- ручной `start_sleep` отменяет pending auto-task

Сна «по длительности из конфига» (`duration_minutes`) нет — длительность передаётся параметром `start_sleep(duration_minutes)` / API и используется только для таймаута (`duration*60 + 300с`).

## Очередь сообщений

Во время сна `queue.py` не батчит, а кладёт сообщения в `sleep_message_queue` (`queue_incoming_message(chat_id, user_id, text)` с текущим `session_id`). Счётчик — `GET /api/sleep/queue-count`. Обработка очереди — после пробуждения/коммита.

## Управление через API

| Эндпоинт | Описание |
|----------|----------|
| `POST /api/sleep/start` | Ручной запуск сна (отклоняется при `forced_awake`) |
| `POST /api/sleep/wake` | Принудительное пробуждение (= rollback, запрещено в `committing`) |
| `GET /api/sleep/status` | Состояние + стадия/прогресс/elapsed/remaining/queue_count |
| `GET /api/sleep/estimate` | Оценка длительности |
| `GET /api/sleep/config` | Конфигурация сна (`start_hour/end_hour/forced_awake`) |
| `POST /api/sleep/config` | Изменение конфигурации |
| `GET /api/sleep/queue-count` | Сообщений в очереди |

## LLM настройки для сна

Единая секция `night_llm` из runtime (отдельного `night_verifier_llm` **нет**):

```json
{
  "night_llm": {
    "model": "...",
    "api_base": "https://openrouter.ai/api/v1",
    "api_key": "...",
    "temperature": 0.3,
    "max_tokens": 4096,
    "tool_choice": "required"
  }
}
```

Наследование: незаданные `model/api_base/api_key` берутся из `llm.*` при старте; `temperature/max_tokens` по умолчанию `0.3/4096`. Верификационный проход использует тот же клиент с t=0.1.

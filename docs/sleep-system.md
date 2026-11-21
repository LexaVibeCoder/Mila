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
scheduled_end_at / completed_at / triggered_by / error_message / version /
wake_requested / processes_json`.

В Observatory фазы сна берутся из этой строки. Пока идёт `reflection`,
у анализа дневника, суммаризации диалогов и извлечения фактов свои статусы.
`dormant` означает, что рефлексия завершена и Мила ждёт пробуждения.
`committing` и `rolling_back` показывают сохранение результатов и откат
соответственно. `scheduled_end_at` — плановое время пробуждения, которое
сохраняется при перезапуске.

На странице `/sleep` кнопка **Разбудить и сохранить результаты** появляется в
фазе `dormant`. Процесс-владелец читает запрос из SQLite, фиксирует результаты
рефлексии и переводит Милу в `awake`. Пока идёт рефлексия, доступно отдельное
действие **Прервать рефлексию и откатить изменения**. Оно останавливает ночные
задачи перед откатом. Запрос привязан к сеансу и фазе, поэтому устаревшая кнопка
не может отменить уже завершённую обработку.

## Запуск и длительность

`start_sleep(duration_minutes=None, triggered_by="manual", note="")`:

- отказывает, если уже `sleeping`/`committing` или `sleep.forced_awake=true`;
- создаёт `session_id`, пишет `sleeping` + presence `sleep`, сбрасывает офсеты
  потребностей;
- пишет в глобальный журнал старт сна: время, планируемая длительность,
  предыдущее состояние (`core/action_journal.py`);
- запускает параллельную рефлексию. Для сна с длительностью обработка может
  продолжаться до пяти минут после планового окончания. По таймауту задачи
  останавливаются и цикл откатывает незавершённые изменения;
- после успешной рефлексии ждёт дедлайна либо ручного пробуждения. Если
  длительность не задана, ждёт ручного пробуждения.

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
- Промпт требует русский текст без китайских/английских слов, с глоссарием
  неоднозначных понятий (`promotion` → «продвижение»/«представление», не «сброс»);
  имена и названия не переводятся. Те же правила (`SUMMARY_LANGUAGE_RULES` и
  `SUMMARY_STYLE_RULES` из `core/llm/summary_guard.py`) использует дневной
  компрессор контекста.
- Стиль сводки (`SUMMARY_STYLE_RULES`): от лица Милы в первом лице, нейтрально
  и фактично, без сленга, оценок и эмоциональной окраски. Модель не пишет
  префикс периода — код добавляет его сам.
- Executor привязан к чату (`StagingExecutor(..., expected_chat_id=chat_id)`):
  чужой `chat_id` в аргументах тула отклоняется с ERROR, ничего не
  стейджится и не удаляется (M3).
- Проверка CJK: если модель вернула китайский текст, тул не выполняется —
  делается ровно один повторный запрос; если снова CJK, диалог пропускается
  с записью причины (`summary in Chinese`), ничего не сохраняется.
- `save_dialog_summary`:
  - `peek_oldest_messages_by_tokens(chat_id, 50000)` — старейшие сообщения
    читаются, но пока не удаляются;
  - `summary_guard.add_period_prefix()` добавляет к тексту локальный период
    `[YYYY-MM-DD — YYYY-MM-DD] ` (один день — `[YYYY-MM-DD] `); без
    разбираемых дат текст не меняется, уже стоящий префикс не дублируется.
    Этот же текст идёт и в `sleep_dialog_summary_log`;
  - staging-операция `context_buffer.chat_*` пишется **до** любых изменений и
    содержит `chat_id`, `summary_text`, `deleted_rows` (строки для восстановления),
    `media` (снимок медиа для quarantine) и позже `summary_entry_id`;
  - `compress_messages_into_summary()` вставляет сводку и удаляет сообщения
    **одной транзакцией** (падение между шагами не даёт ни дубля сводки, ни
    потери сообщений); `messages_removed` = реальный `rowcount` удаления;
  - медиа summarized-away сообщений переносится в
    `data/media/quarantine/{session_id}/` (файлы не удаляются до commit):
    `media_path` в `chunks`/`media_logs` очищается, пути восстанавливаются
    на rollback, quarantine удаляется на commit; каталог создаётся с правами
    `0700`, а `session_id` валидируется как UUID (иначе — sanitized-токен);
  - пишет `sleep_dialog_summary_log`;
- Даты в промпте (`[YYYY-MM-DD]`) конвертируются в локальную зону из UTC через
  `buffer.format_created_at_local()` (S12).
- Верификация: approve/reject; проверяются точность, краткость (2–4 предложения)
  и нейтральное изложение от первого лица; префикс периода код добавляет сам,
  поэтому верификатор не должен отклонять сводку из-за него. При rejection
  результат помечается rejected.

### 3. Извлечение фактов (`process_user_fact_extraction`)

- Берёт последние 200 `chunks` каждого активного чата.
- Тулзы: `add_user_fact`, `skip_user_fact_extraction`.
- `add_user_fact` пишет в `memories` (source_chat_id обязателен) и ставит
  staging-операцию `insert` на `memories`.
- Поддерживает групповые чаты: в промпт попадает **полный** `user_id` из чанка
  (`[user_id]: текст`). Обрезанные префиксы (`65660908...`) не используются:
  `resolve_fact_user_id()` принимает полный id или уникальный префикс и
  отклоняет неизвестный/неоднозначный id, чтобы не создавать ghost-пользователей.
- Если факт создаёт новую строку `users`, ставится отдельная staging-операция
  на `users`. Rollback удаляет её только при отсутствии ссылок; профили,
  существовавшие до сессии, не затрагиваются.
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
- `insert` по `users`;
- `insert` summary в `context_buffer.chat_*`;
- `insert` по `diary_entries` — снимок дневника перед очисткой в commit (M2).

Порядок важен: id (`chunk_id`, `mem_id`) и undo-payload генерируются **до**
изменения БД, staging-строка пишется первой, затем выполняется insert с этим
id. Так падение между staging и insert оставляет безвредную no-op запись, а не
неучтённую строку в целевой таблице. Payload сводки дописывается
(`update_staged_payload`) после появления `summary_entry_id` и реального
`messages_removed`.

### Commit (`commit_all`)

1. `state = committing`;
2. `snapshot_diary_entries()` читает активные записи и сохраняет их в staging
   (`target_table='diary_entries'`, payload `rows`) для восстановления после сбоя;
3. `clear_diary(diary_snapshot)` сохраняет отдельную копию снимка в
   `LOG_DIR/diary_logs/`, сравнивает его с текущими строками и архивирует их
   в SQLite. Если бэкап не удался или строки изменились, `commit_all`
   возвращает `False` и `execute_sleep` запускает rollback;
4. все staging-операции (включая снимок дневника) помечаются `applied=1`.
   Если архивирование или пометка завершились ошибкой, снимок восстанавливается
   через `restore_diary_entries()`. После падения процесса то же делает recovery;
5. удаляется `data/media/quarantine/{session_id}/`;
6. `gc_staged_operations()` — applied-записи старше 30 дней удаляются;
7. `state = awake`, presence → `rest`;
8. в журнал пишется пробуждение с реальной длительностью сна
   (`now - started_at`).

Сами данные уже лежат в целевых таблицах; commit фиксирует их и убирает
возможность rollback.

### Rollback (`rollback_all`)

1. Сначала откатываются операции, которым нужны **свои** соединения:
   `context_buffer.chat_*` (ссылки медиа пишутся вторым соединением в
   `mila.db`) и `diary_entries` (context_buffer.db). Раньше внешняя
   write-транзакция `mila.db` блокировала их на `busy_timeout`, а ошибка
   проглатывалась — теперь они идут до открытия внешнего соединения (B1).
2. Затем — операции по `mila.db` в одном соединении, в обратном порядке:
   - `insert` chunk → удалить строку;
   - `delete` chunk → `is_active=1`;
   - `insert` memory → удалить `mem_id`;
   - `update` chunk **не откатывается автоматически** (пишется предупреждение).
3. Undo сводки (`context_buffer.chat_*`): вернуть файлы из quarantine и ссылки
   `chunks.media_path`/`media_logs`, удалить сводку (`summary_entry_id`, при
   его отсутствии — по точному тексту), восстановить `deleted_rows` с их
   исходными `id`. Каждый шаг под своим try/except, чтобы одна ошибка не
   отменяла остальные (M1).
4. Если хоть один шаг упал или в quarantine остались файлы: staging-записи
   **сохраняются**, quarantine **не** удаляется, `state = sleep_error` с
   `error_message` — recovery повторит rollback. `rolled_back=1` в
   `sleep_dialog_summary_log` ставится в любом случае.
5. Успешный rollback: очищает staging сессии, удаляет quarantine-каталог,
   **дневник не трогает** (анализ не был зафиксирован; архивирует и очищает его
   только commit), `state = awake`, presence → `rest` (`journal=False`), в
   журнал пишется пробуждение с реальной длительностью сна.

## Crash Recovery

`MilaUserbot.start()` → `recover_from_crash()`:

- `awake` → синхронизировать устаревший presence после завершения сна;
- `rolling_back` → повторить прерванный откат;
- `sleeping`:
  - `dormant` (или `idle` с прогрессом ≥ 89 в старой БД) → продолжить
    ожидание. Если дедлайн истёк или уже запрошено пробуждение, выполнить commit;
  - прерванная рефлексия → `rollback_all()`;
- `committing`:
  - staging пуст → всё применено → `awake` (+ событие пробуждения в журнал);
  - иначе → rollback (+ событие пробуждения в журнал) — в том числе когда крах
    случился между `clear_diary()` и пометкой staging: дневник вернётся из
    staging-записи `diary_entries` (M2);
- `sleep_error` → rollback (в том числе повторный rollback после незавершённой
  попытки, см. M1).

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
  с длительностью до конца окна. После завершения сна или ручного пробуждения
  планировщик ждёт следующего окна.

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
| `POST /api/sleep/wake` | Запрос пробуждения (`session_id`, `mode`). `keep` фиксирует результаты после рефлексии, `rollback` останавливает незавершённую рефлексию и откатывает изменения |
| `GET /api/sleep/status` | Состояние, фаза, статусы параллельных задач (`processes`), запрос на пробуждение (`wake_requested`), время и очередь |
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
    "tool_choice": "required",
    "reasoning_effort": "none",
    "extra_body": {}
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
  в Observatory;
- `night_llm.reasoning_effort` (по умолчанию `none`) применяется и к
  генератору, и к верификатору; `night_llm.extra_body` добавляет
  провайдерские поля. Меняется через Observatory `/model` без рестарта.

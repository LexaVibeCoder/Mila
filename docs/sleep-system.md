# Ночная рефлексия (Sleep System)

Система ночной рефлексии — асинхронный фоновый процесс, запускаемый по расписанию или вручную. Состоит из трёх параллельных процессов, каждый использует двухстадийную LLM-проверку.

## Архитектура

```
core/sleep_manager.py   — оркестратор
core/sleep_processes.py — 3 процесса
core/sleep_state.py     — машина состояний + SQLite
core/sleep_llm_tools.py — определения тулзов для LLM
```

## Машина состояний

```
AWAKE ──→ SLEEPING ──→ COMMITTING ──→ AWAKE
                         │
                         └── SLEEP_ERROR ──→ AWAKE (rollback)
```

Состояния хранятся в таблице `sleep_state` в `mila.db`.

## Процессы (запускаются параллельно через `asyncio.gather`)

### 1. Анализ дневника (`process_diary_analysis`)

**Цель**: LLM читает дневник Милы + её вечную память и решает, нужно ли добавить/обновить/удалить чанки в `mila_memory_chunks`.

**Тулзы**:
- `add_memory_chunk(title, content_md, importance)` — создать чанк
- `update_memory_chunk(chunk_id, title, content_md, importance)` — обновить
- `delete_memory_chunk(chunk_id)` — мягкое удаление

**Верификация**: второй LLM (t=0.1) проверяет предложенные изменения через `approve_changes` / `reject_changes`. При rejection → ретрай генерации (до 3 попыток).

### 2. Суммаризация диалогов (`process_dialog_summarization`)

**Цель**: Для каждого чата с новыми сообщениями с прошлого сна LLM создаёт краткую суммаризацию (2-4 предложения) или пропускает диалог.

**Тулзы**:
- `save_dialog_summary(chat_id, summary)` — сохранить
- `skip_dialog(chat_id)` — пропустить

После суммаризации старые сообщения удаляются из контекстного буфера.

### 3. Извлечение фактов о пользователях (`process_user_fact_extraction`)

**Цель**: Для каждого активного диалога LLM извлекает долгосрочные факты о пользователях.

**Тулзы**:
- `add_user_fact(user_id, fact_text, confidence)` — добавить факт
- `skip_user_fact_extraction()` — пропустить

Поддерживает групповые чаты (определение пользователя по `from_user_id`).

## Стейдж и атомарность

Все изменения СНАЧАЛА записываются в `sleep_transaction_staging`:

- Добавленные чанки (с временным `faiss_id`)
- Обновлённые чанки (старое содержание сохраняется для отката)
- Удалённые чанки (реактивация при откате)
- Добавленные воспоминания
- Очистка дневника

**Commit**: все staged → applied, дневник сохраняется в файл, очищается, состояние → AWAKE
**Rollback**: обратные операции → восстановление исходного состояния

## Crash Recovery

При старте `bot.py` вызывает `recover_from_crash()`:
1. Проверяет состояние `sleep_state`
2. Если SLEEPING/COMMITTING/SLEEP_ERROR → `rollback_all()`
3. Устанавливает AWAKE

## Auto-sleep

`auto_sleep_scheduler()` — фоновый цикл, проверяет каждые 60 секунд:

```python
start_hour = runtime_config.get("sleep.start_hour")  # например, 23
end_hour = runtime_config.get("sleep.end_hour")      # например, 07
```

Если текущий час в диапазоне → `start_sleep(triggered_by="automatic")`.

## Очередь сообщений

Во время сна сообщения не теряются — они помещаются в `sleep_message_queue` и обрабатываются после пробуждения.

## Управление через API

| Эндпоинт | Описание |
|----------|----------|
| `POST /api/sleep/start` | Ручной запуск сна |
| `POST /api/sleep/wake` | Принудительное пробуждение |
| `GET /api/sleep/status` | Текущее состояние |
| `GET /api/sleep/estimate` | Оценка длительности |
| `GET /api/sleep/config` | Конфигурация сна |
| `POST /api/sleep/config` | Изменение конфигурации |
| `GET /api/sleep/queue-count` | Сообщений в очереди |

## LLM настройки для сна

Используют отдельную конфигурацию из runtime:

```json
{
  "night_llm": {
    "model": "model-name",
    "api_base": "http://...",
    "api_key": "...",
    "temperature": 0.3
  },
  "night_verifier_llm": {
    "model": "model-name",
    "api_base": "http://...",
    "api_key": "...",
    "temperature": 0.1
  }
}
```

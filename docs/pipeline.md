# Основной пайплайн (ThinkPipeline)

**Файл**: `core/pipeline.py`

`ThinkPipeline` — ядро рассуждения Милы. Это одностадийный цикл, в котором LLM вызывается с `tool_choice` из runtime (`"required"` по умолчанию, переключается на `"auto"` из Observatory) и должна вызвать функцию `think()`, содержащую `thoughts` + JSON-список действий.

## Логика работы

```
process_message_stream() / process_message()
    │
    ├── Сборка system_prompt (persona + контекст + presence-блок) — один раз на запуск
    ├── tool_definitions = get_tool_definitions() — с учётом runtime tools.*
    ├── messages = list(agent_internal_history)
    │       (текущее сообщение уже в буфере; дублировать user_message НЕ нужно,
    │        токенный лимит уже применён в _build_histories)
    ├── Если передан media (photo/voice текущего уведомления):
    │       _attach_media() — последнее user-сообщение становится
    │       OpenAI content blocks (текст + image_url + input_audio);
    │       история остаётся текстовой (плейсхолдеры). Плюс system-строка
    │       с описанием события (llm_event_line)
    │
    ├── while iteration < MAX_THINK_ITERATIONS (5):
    │   │
    │   ├── 1. tool_choice = runtime_config.get("llm.tool_choice", "required")
    │   ├── 2. LLM.generate(..., tool_choice) → think()
    │   ├── 3. Валидация вызова think()
    │   │       └── Нет tool_calls или вызвана не think():
    │   │           итерация 1 → system-ошибка в messages + 1 ретрай,
    │   │           итерация 2+ → break
    │   ├── 4. _parse_think_call() → список actions (пусто → break)
    │   ├── 5. Для каждого action → _execute_single_action():
    │   │       ├── send_message → готов к отправке (отправляет bot/queue)
    │   │       ├── search_memory(status=searched) → инъекция результатов → повтор
    │   │       ├── enter_dialog/enter_rest/enter_working(status=switched) →
    │   │       │   инъекция "Presence switched..." → повтор в новом состоянии
    │   │       ├── start_sleep(status=sleeping) → остановка (как silence)
    │   │       ├── silence → остановка пайплайна
    │   │       ├── write_diary / update_diary → запись в дневник
    │   │       └── после каждого: NeedsTracker.apply_action_effect(action_type)
    │   └── 6. Если был search_memory со статусом searched
    │           или presence-переключение:
    │           └── инъекция результатов → повтор цикла
    │       Иначе (или silence / start_sleep):
    │           └── break — done
    │
    └── yield "done" или "error" (stream) / return result (обычный)
```

## Формат think()

LLM возвращает tool_call `think` с JSON. **Ключ `thoughts` обязан идти ПЕРЕД `actions`** (требование промпта; в логах порядок нормализуется `_reorder_think_args`):

```json
{
  "thoughts": "краткое рассуждение",
  "actions": [
    { "action_type": "send_message", "params": { "text": "текст", "chat_id": "123" } },
    { "action_type": "search_memory", "params": { "query": "что ищем" } }
  ]
}
```

### Поля

- `thoughts` — внутреннее рассуждение (в fallback-отправке может стать текстом ответа)
- `actions` — массив действий для исполнения
- Старого поля `is_direct_answer` **нет** в коде — игнорируется, если модель его вернёт

## Доступные action_type

| Тип | Описание | Параметры |
|-----|----------|-----------|
| `send_message` | Отправить сообщение в чат | `text: str`, `chat_id?: str` (по умолчанию текущий) |
| `search_memory` | Поиск в FAISS-памяти (top-10 → rerank → top-5 в контекст) | `query: str` |
| `silence` | Промолчать, остановить пайплайн | `reason: str` |
| `write_diary` | Новая запись в дневник | `entry: str` (fallback `text`), `category?: day_entries \| reflection_entries` |
| `update_diary` | Обновить запись дневника по ID из промпта | `entry: str`, `entry_id: str`, `category?` |
| `enter_dialog` | Перейти в диалог с человеком | `chat_id!: str`, `note?` → повтор LLM в новом состоянии |
| `enter_rest` | Вернуться в отдых | `note?` → повтор LLM |
| `enter_working` | Заняться своим делом | `note?` → повтор LLM |
| `start_sleep` | Лечь спать (запускает ночной цикл) | `duration_minutes!: int` → стоп как silence |

Набор `enum` собирается динамически: отключённые в runtime тулзы исключаются (`tools.presence` — все четыре presence-тула разом); если выключены все — остаётся `silence`.

## ToolExecutor

**Файл**: `core/tools/executor.py`

```python
executor = ToolExecutor(
    text_memory=self.text_memory,
    embedding_client=self.emb_client,
    current_user_id=self.user_id,
    chat_id=self.chat_id,
)
result = await executor.execute_action(action)
```

- `_handle_send_message` — только готовит `{type, text, chat_id, status: "ready"}`; в Telegram отправляет `core/queue.py`, затем пишет `chunks` + `push_message(..., role="assistant")`
- `_handle_search_memory` — эмбеддинг → `VectorMemory.search(k=10)` → `search_memories_by_faiss_ids` → `RerankerClient.rerank` → топ-5 в `context_addon`; статусы `searched` / `error`
- `_handle_silence` → `{status: "silent"}`; пайплайн останавливается сразу
- `_handle_write_diary` → `add_diary_entry(entry, category)`; принимает `entry` или `text`
- `_handle_update_diary` → `update_diary_entry(entry_id, entry, category)`; статусы `updated` / `not_found` / `error`

## Управление тулзами

Тулзы включаются/выключаются через runtime config (`./runtime_config.json`):

```json
{
  "tools": {
    "search_memory": true,
    "send_message": true,
    "silence": true,
    "write_diary": true,
    "update_diary": true
  }
}
```

Отключённые тулзы не попадают в `get_tool_definitions()` и недоступны LLM. Переключение — `PUT /api/tools/{name}` или страница Tools в Observatory, применяется со следующего вызова LLM.

## Логирование

Каждый запуск сохраняется в `logs/pipeline_*.json` (`LOG_DIR` из config, по умолчанию `logs/`):

```json
{
  "timestamp": "2026-01-01T12:00:00",
  "user_id": "...",
  "chat_id": "...",
  "user_message": "...",
  "system_prompt": "...",
  "llm_messages": [...],
  "thoughts": "...",
  "actions": [...],
  "raw_llm_responses": [
    { "iteration": 1, "thoughts": "...",
      "tool_calls": [{ "name": "think", "arguments": "{...}" }],
      "finish_reason": "tool_calls" }
  ],
  "success": true,
  "error": null,
  "timing": { "total": "3.21s", "iterations": 2 }
}
```

Просмотр — `GET /api/pipeline/logs`, `/api/pipeline/logs/{file}`, группировка по чатам — `GET /api/pipeline/chats`, UI — страница Pipeline в Observatory. Детальный эндпоинт санитизирует мультимодальные `llm_messages` в plain-text (`[фото]`, `[голосовое сообщение]`, base64 выкидывается — в файле лога на диске всё сохраняется целиком), поэтому UI не падает на content-блоках (React error #31).

## Потоковая обработка

`process_message_stream()` — асинхронный генератор событий (именно его потребляет `core/queue.py`):

```
thought       → {"type": "thought", "thoughts": "рассуждение"}
action        → {"type": "action", "action": <результат execute_action>}
search_result → {"type": "search_result", "content": "context_addon"}
done          → {"type": "done", "result": {...}}
error         → {"type": "error", "error": "описание"}
```

Не-стрим `process_message()` выполняет тот же цикл и возвращает `result` целиком.

В `queue.py` события обрабатываются так:
- `send_message` (непустой `text`) → пауза 0.3–1.0с → отправка в Telegram → `add_chunk` + `push_message(role="assistant")`
- `silence` → флаг, отправки не будет; пустой ответ сохраняется как `content=""` с `llm_full_response`
- `write_diary` / `update_diary` → только лог (запись уже выполнена экзекутором)
- `done.thoughts` без `send_message` и без `silence` → fallback: если `thoughts` длиннее 80 символов — автоотправка, иначе сохранение как молчаливого ответа
- новое сообщение или печать собеседника **во время** генерации/перед отправкой → отмена прогона, рестарт батча (см. [messaging.md](messaging.md))

## Важные константы

| Константа | Значение | Где | Описание |
|-----------|----------|-----|----------|
| `MAX_THINK_ITERATIONS` | 5 | `core/pipeline.py` | Максимум итераций цикла |
| `CONTEXT_MAX_TOKENS` | 30000 | `config.py` | Лимит контекстного буфера (сообщения; суммари вне лимита частично — см. context-buffer) |
| `MAX_CONTEXT_TOKENS` | 10000 | `config.py` / `runtime pipeline.max_context_tokens` | Целевой лимит контекста |
| `MAX_STM_MESSAGES` | 30 | `config.py` | Лимит `get_context()` по умолчанию |
| `MAIN_LLM_MAX_TOKENS` | 2048 | `config.py` | Дефолт `max_tokens` генерации |
| `MAIN_LLM_TEMPERATURE` | 0.7 | `config.py` | Дефолт температуры (перекрывается runtime `llm.temperature`) |

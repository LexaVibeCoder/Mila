# Система тулзов (Tools)

**Файл**: `core/tools/executor.py`

Система инструментов — механизм, через который LLM взаимодействует с внешним миром. Модель вызывает функцию `think()` на каждом шаге пайплайна (`tool_choice` из runtime: `"required"` по умолчанию, `"auto"` — опция из Observatory).

## Определения тулзов

`get_tool_definitions()` возвращает OpenAI-совместимые definitions. Читает `runtime_config.get("tools")`, исключает отключённые. Если выключены все — остаётся `silence`, чтобы модель всегда могла завершить ход.

### think()

Единственная функция, доступная LLM:

```json
{
  "name": "think",
  "description": "Express your internal thoughts and optionally perform actions. You MUST call this function EVERY turn. 'thoughts' MUST appear BEFORE 'actions'.",
  "parameters": {
    "type": "object",
    "properties": {
      "thoughts": { "type": "string", "description": "Private reasoning, never visible to user" },
      "actions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "action_type": {
              "type": "string",
              "enum": ["send_message", "search_memory", "silence", "write_diary", "update_diary",
                       "enter_dialog", "enter_rest", "enter_working", "start_sleep"]
            },
            "params": {
              "type": "object",
              "description": "Parameters for the action",
              "properties": {
                "chat_id": { "type": "string", "description": "Telegram chat ID (send_message). По умолчанию текущий чат" },
                "text": { "type": "string", "description": "Текст сообщения (только send_message)" },
                "query": { "type": "string", "description": "Поисковый запрос (только search_memory)" },
                "reason": { "type": "string", "description": "Причина молчания (только silence)" },
                "entry": { "type": "string", "description": "Текст записи 1-3 предложения от первого лица (write_diary / update_diary)" },
                "category": { "type": "string", "enum": ["day_entries", "reflection_entries"], "description": "Категория дневника" },
                "entry_id": { "type": "string", "description": "ID записи из блока дневника в промпте (только update_diary)" },
                "duration_minutes": { "type": "integer", "description": "На сколько минут спать (только start_sleep, обязателен)" },
                "note": { "type": "string", "description": "Причина/комментарий (presence-тулы)" }
              }
            }
          },
          "required": ["action_type", "params"]
        }
      }
    },
    "required": ["thoughts", "actions"]
  }
}
```

Состав `enum` динамический — равен включённым в runtime тулзам.

## Доступные типы действий

### send_message

```json
{ "action_type": "send_message", "params": { "text": "Привет! Как дела?", "chat_id": "123" } }
```

**Параметры**: `text` (обязателен, непустой), `chat_id` (опционален — по умолчанию текущий чат).

**Обработка** (две стадии):
1. `ToolExecutor._handle_send_message` только возвращает `{type, text, chat_id, status: "ready"}` — в Telegram **не отправляет**.
2. `core/queue.py` отправляет через `tg_adapter.send_text()`, затем `add_chunk(..., type='raw')` + `push_message(..., role="assistant")`.

### search_memory

```json
{ "action_type": "search_memory", "params": { "query": "Что Мила думает про программирование?" } }
```

**Pipeline** (`_handle_search_memory`):
1. Эмбеддинг запроса через `EmbeddingClient`
2. Поиск top-10 в FAISS (`VectorMemory.search(k=10)`, отсев `idx == -1`)
3. Загрузка содержимого из `memories` по `faiss_id`
4. Переранжирование через `RerankerClient` (BGE CrossEncoder, порог `RERANKER_THRESHOLD = 0.6`)
5. Топ-5 в `context_addon` → инъекция `Search results in memory:\n...` в messages → повторный вызов LLM
6. Статусы: `searched` (повтор цикла) / `error` (цикл завершается)

**Runtime config**: `tools.search_memory` — вкл/выкл.

### silence

```json
{ "action_type": "silence", "params": { "reason": "Нет необходимости отвечать" } }
```

Останавливает пайплайн немедленно (остальные actions в списке не исполняются). В `queue.py`:
- `has_silence_action = True` → отправки нет, ответ сохраняется как `content=""` с `llm_full_response=thoughts`;
- авто-silence до LLM: при `fatigue > 0.85` ночью (см. [messaging.md](messaging.md)).

### write_diary

```json
{ "action_type": "write_diary", "params": { "entry": "Сегодня был интересный разговор о...", "category": "day_entries" } }
```

Вызывает `add_diary_entry(entry, category)` из `core/diary/manager.py`. Поле `entry` обязательно; пустое `entry` fallback'ится на `text` (защита от путаницы модели), `category` вне `day_entries|reflection_entries` → `day_entries`. Возвращает `{entry_number, entry_id, category, status: "written"}`.

### update_diary

```json
{ "action_type": "update_diary", "params": { "entry": "Уточнённый текст", "entry_id": "2", "category": "reflection_entries" } }
```

Вызывает `update_diary_entry(entry_id, entry, category)`; ID берётся из блока `[ID: N]` в system prompt. Статусы: `updated` / `not_found` / `error` (нет `entry` или `entry_id`). `category` опционален — без него меняется только текст.

### Presence-тулы (состояния Милы)

Переключают `core/presence.py`. После `enter_*` со статусом `switched` пайплайн инжектит системное сообщение и **перевызывает LLM**, чтобы она решила что делать уже в новом состоянии. `start_sleep` со статусом `sleeping` останавливает пайплайн как `silence`.

```json
{ "action_type": "enter_dialog", "params": { "chat_id": "123", "note": "хочу поговорить" } }
{ "action_type": "enter_rest", "params": {} }
{ "action_type": "enter_working", "params": { "note": "пишу главу" } }
{ "action_type": "start_sleep", "params": { "duration_minutes": 480 } }
```

- `enter_dialog` — `chat_id` обязателен (чат человека из уведомления); имя подтягивается из `users`. Возвращает `{chat_id, user_name, state, status: "switched"}` или `error` без `chat_id`.
- `enter_rest` — возврат в отдых (дефолт), сбрасывает фокус.
- `enter_working` — своё дело; в v1 видимость уведомлений как у rest.
- `start_sleep` — `duration_minutes` обязателен (1–720, клиппится); пишет намерение в presence и `await start_sleep(duration, triggered_by="mila_tool")` — дальше работает ночной цикл (рефлексия, запоминание). Ошибка sleep manager → `{status: "error"}`.

**Runtime config**: `tools.presence` — вкл/выкл все четыре разом; если выключены все тулзы вообще — остаётся `silence`.

## ToolExecutor

**Класс**: `ToolExecutor` в `core/tools/executor.py`

```python
class ToolExecutor:
    def __init__(self, text_memory, embedding_client, current_user_id: str, chat_id: str):
        ...

    async def execute_action(self, action) -> dict:
        # роутинг по action_type на _handle_*; неизвестный → {"status": "unknown"}
```

Каждый хендлер возвращает dict с `type` + `status` для пайплайна и логов (`pipeline_*.json`).

После каждого исполненного action пайплайн вызывает `NeedsTracker.apply_action_effect(action_type)` (социалка/усталость/скука корректируются офсетами): `enter_dialog` ≈ `send_message` (social −0.1), `enter_rest`/`start_sleep` ≈ `silence` (fatigue −0.05/−0.1).

## Пайплайн интеграция

```
LLM → think(thoughts, actions) → парсинг JSON → список actions
                                                  │
                     ┌────────────┬───────────────┼───────────────┬───────────────┐
                     ▼            ▼               ▼               ▼               ▼
             send_message   search_memory      silence    write/update_diary  presence-тулы
                     │            │               │               │               │
                     ▼            ▼               ▼               ▼               ▼
              queue.send    FAISS top-10 →   остановка      diary_entries   enter_*: повтор LLM
              в Telegram    rerank → top-5    пайплайна      + daily .md     в новом состоянии
                     │            │                                          start_sleep: стоп
                     ▼            ▼                                           (ночной цикл)
               chunks +      результаты →
               контекст      инъекция →
                             повтор LLM
```

## Управление через API

`PUT /api/tools/{tool_name}` — включить/выключить тулз:

```json
{ "enabled": false }
```

Изменения применяются немедленно — при следующем вызове `get_tool_definitions()` исключит отключённые тулзы из `enum`. UI — страница Tools в Observatory.

## Специальные тулзы для сна

В `core/sleep/tools.py` определены отдельные наборы тулзов для каждого из трёх ночных процессов (`add/update/delete_memory_chunk`, `save_dialog_summary/skip_dialog`, `add_user_fact/skip_user_fact_extraction`, `approve/reject_changes`). Они НЕ доступны в основном пайплайне — только во сне (см. [sleep-system.md](sleep-system.md)).

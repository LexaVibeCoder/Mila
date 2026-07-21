# Основной пайплайн (ThinkPipeline)

**Файл**: `core/pipeline.py`

`ThinkPipeline` — ядро рассуждения Милы. Это одностадийный цикл, в котором LLM вызывается с `tool_choice="required"` и должен вызвать функцию `think()`, содержащую JSON-список действий.

## Логика работы

```
process_message_stream()
    │
    ├── while iteration < MAX_THINK_ITERATIONS (5):
    │   │
    │   ├── 1. Сборка system_prompt (persona + контекст)
    │   ├── 2. LLM.generate(..., tool_choice="required")
    │   │       └── Модель ОБЯЗАНА вызвать think()
    │   ├── 3. Валидация вызова think()
    │   │       └── Если ошибка → 1 ретрай
    │   ├── 4. _parse_think_call() → список actions
    │   ├── 5. Для каждого action:
    │   │       ├── send_message → yield "action" (отправка в Telegram)
    │   │       ├── search_memory → yield "search_result"
    │   │       ├── silence → yield "action"
    │   │       └── write_diary → запись в дневник
    │   └── 6. Если был search_memory:
    │           └── инъекция результатов → повтор цикла
    │       Иначе:
    │           └── break — done
    │
    └── yield "done" или "error"
```

## Формат think()

LLM возвращает tool_call с JSON:

```json
{
  "thought": "краткое рассуждение",
  "actions": [
    {
      "action_type": "send_message",
      "params": { "message": "текст сообщения" }
    },
    {
      "action_type": "search_memory",
      "params": { "query": "что ищем" }
    }
  ],
  "is_direct_answer": false
}
```

### Поля

- `thought` — внутреннее рассуждение (не показывается пользователю)
- `actions` — массив действий для исполнения
- `is_direct_answer` — флаг прямого ответа (если true, Мила не ждёт верификации)

## Доступные action_type

| Тип | Описание | Параметры |
|-----|----------|-----------|
| `send_message` | Отправить сообщение в чат | `message: str` |
| `search_memory` | Поиск в FAISS-памяти | `query: str` |
| `silence` | Промолчать (не отвечать) | `reason: str` |
| `write_diary` | Запись в дневник | `entry: str` |

## ToolExecutor

**Файл**: `core/tools.py`

Исполняет действия из think():

- `_handle_send_message` — сохраняет сообщение в `chunks`, добавляет в контекст
- `_handle_search_memory` — эмбеддинг запроса → поиск в FAISS → переранжирование BGE → обогащение контекста
- `_handle_silence` — логирует причину молчания
- `_handle_write_diary` — вызывает `add_diary_entry()`

## Управление тулзами

Тулзы можно включать/отключать через runtime config (`data/runtime_config.json`):

```json
{
  "tools": {
    "search_memory": true,
    "send_message": true,
    "silence": true,
    "write_diary": true
  }
}
```

Отключённые тулзы не передаются в definition и недоступны LLM.

## Логирование

Каждый запуск пайплайна сохраняется в `logs/pipeline_*.json`:

```json
{
  "timestamp": "2024-01-01T12:00:00",
  "user_message": "...",
  "iterations": [
    {
      "thought": "...",
      "actions": [...],
      "search_results": [...]
    }
  ]
}
```

## Потоковая обработка

`process_message_stream()` — асинхронный генератор, возвращающий события:

```
thought   → {"type": "thought", "content": "рассуждение"}
action    → {"type": "action", "action_type": "send_message", ...}
search_result → {"type": "search_result", "results": [...]}
done      → {"type": "done", "response": "итоговый ответ"}
error     → {"type": "error", "error": "описание ошибки"}
```

В `bot.py` эти события обрабатываются:
- `send_message` → `client.send_message()` (мгновенная отправка)
- `silence` → пропуск отправки
- Если ни одного `send_message` не было, но thought длинный → auto-send fallback

## Важные константы

| Константа | Значение | Описание |
|-----------|----------|----------|
| `MAX_THINK_ITERATIONS` | 5 | Максимум итераций цикла |
| `MAX_CONTEXT_TOKENS` | ~30000 | Лимит токенов контекста |
| `MAX_STM_MESSAGES` | 50 | Лимит сообщений в истории |

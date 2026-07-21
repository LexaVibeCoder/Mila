# Система тулзов (Tools)

**Файл**: `core/tools.py`

Система инструментов (тулзов) — механизм, через который LLM взаимодействует с внешним миром. Модель **обязана** вызывать функцию `think()` на каждом шаге пайплайна (`tool_choice="required"`).

## Определения тулзов

`get_tool_definitions()` возвращает OpenAI-совместимые definitions. Считывает runtime config, чтобы исключить отключённые тулзы.

### think()

Единственная функция, доступная LLM:

```json
{
  "name": "think",
  "description": "Проанализируй ситуацию и верни JSON с действиями",
  "parameters": {
    "type": "object",
    "properties": {
      "thought": {
        "type": "string",
        "description": "Внутреннее рассуждение"
      },
      "actions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "action_type": {
              "type": "string",
              "enum": ["send_message", "search_memory", "silence", "write_diary"]
            },
            "params": {
              "type": "object"
            }
          },
          "required": ["action_type", "params"]
        }
      },
      "is_direct_answer": {
        "type": "boolean"
      }
    },
    "required": ["thought", "actions"]
  }
}
```

## Доступные типы действий

### send_message

Отправка сообщения в текущий чат.

```json
{ "action_type": "send_message", "params": { "message": "Привет! Как дела?" } }
```

**Обработка**:
- Сохраняет сообщение в `chunks` (mila.db) с `type="assistant"` и `llm_full_response`
- Добавляет в контекстный буфер
- В `bot.py` — реальная отправка в Telegram через `client.send_message()`

### search_memory

Поиск релевантных воспоминаний в FAISS.

```json
{ "action_type": "search_memory", "params": { "query": "Что Мила думает про программирование?" } }
```

**Pipeline**:
1. Эмбеддинг запроса через `EmbeddingClient`
2. Поиск top-K в FAISS (`VectorMemory.search()`)
3. Загрузка содержимого из `memories` по `faiss_id`
4. Переранжирование через `RerankerClient` (BGE CrossEncoder, порог 0.6)
5. Инъекция результатов контекст → повторный вызов LLM

**Runtime config**: `tools.search_memory` — вкл/выкл

### silence

Промолчать — не отвечать на сообщение.

```json
{ "action_type": "silence", "params": { "reason": "Нет необходимости отвечать" } }
```

В `bot.py`:
- Если action содержит silence → `_should_silence = True`
- После цикла: если хотя бы один `send_message` был → отправка
- Если ни одного `silence` не было, но цикл завершён → fallback auto-send (если thought длиннее 50 символов)

### write_diary

Запись в дневник Милы.

```json
{ "action_type": "write_diary", "params": { "entry": "Сегодня был интересный разговор о..." } }
```

Вызывает `add_diary_entry()` из `core/diary.py`.

## ToolExecutor

**Класс**: `ToolExecutor` в `core/tools.py`

```python
class ToolExecutor:
    def __init__(self, text_memory, embedding_client, user_id, chat_id, my_user_id):
        ...

    def execute_action(self, action) -> ActionResult:
        # роутинг по action_type
        handler = getattr(self, f"_handle_{action['action_type']}")
        return handler(action['params'])
```

Каждый хендлер возвращает `ActionResult` с результатом выполнения для логирования.

## Пайплайн интеграция

```
LLM → think() → парсинг JSON → список actions
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
            send_message   search_memory   silence
                    │            │            │
                    ▼            ▼            ▼
              Telegram.send  FAISS search   пропуск
                    │            │
                    ▼            ▼
              сохранено     результаты →
              в контекст     инъекция →
                             повтор LLM
```

## Управление через API

`PUT /api/tools/{tool_name}` — включить/выключить тулз:

```json
{ "enabled": false }
```

Изменения применяются немедленно — при следующем вызове `get_tool_definitions()` исключит отключённые тулзы из definitions.

## Специальные тулзы для сна

В `core/sleep_llm_tools.py` определены отдельные наборы тулзов для каждого из трёх ночных процессов. Они НЕ доступны в основном пайплайне — только во сне.

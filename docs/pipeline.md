# Основной пайплайн (ThinkPipeline)

**Файл:** `core/pipeline.py`

`ThinkPipeline` — ядро рассуждения Милы. Это **direct-tools** цикл: LLM получает
реальные функции (`enter_dialog`, `send_message`, `search_memory`, ...), а не
обёртку `think()`. В диалоге ответ приходит обычным JSON-контентом
`{"messages": [...]}`, пустой массив означает молчание.

Название `ThinkPipeline` сохранено для совместимости; алиас — `MilaPipeline`.

## Логика работы

```
process_message_stream() / process_message()
    │
    ├── req_user / req_chat — снимок хода (параллельные чаты не перетирают self.*)
    ├── messages = list(agent_internal_history)
    │   ├── proactive_block? → system-блок с инструкциями и списком чатов
    │   └── иначе → notification-блок для rest/working (последние 2000 символов)
    ├── _attach_media(): текущие фото/аудио + последние 3 фото и 2 аудио из истории
    │
    ├── while iteration < max_iterations (runtime, по умолчанию 7):
    │   ├── presence = get_presence()               (может меняться внутри цикла)
    │   ├── system_prompt = build_main_llm_system_prompt(...)  (заново каждый раз)
    │   ├── tools = get_tool_definitions(presence_state, include_proactive=...)
    │   ├── если state == "dialog": response_format = json_schema(chat_messages)
    │   ├── LLM.generate(messages, system_prompt, tools, tool_choice="auto")
    │   │
    │   ├── нет tool_calls:
    │   │   ├── dialog → parse_chat_messages(content)
    │   │   │   ├── невалидный JSON → system-reminder + ретрай
    │   │   │   ├── [] → silence
    │   │   │   └── 1–3 пузыря → reply
    │   │   └── rest/working:
    │   │       ├── пусто → silence
    │   │       └── текст → нарушение протокола → reminder + ретрай
    │   │
    │   └── есть tool_calls:
    │       ├── контент этого хода НЕ отправляется (это рассуждение)
    │       ├── для каждого action: execute_action → apply_action_effect
    │       ├── start_sleep(sleeping) → silence
    │       ├── enter_*/switched → system-сообщение → повтор в новом состоянии
    │       ├── send_message(ready) → кандидат на финальный ответ
    │       ├── search_memory → инъекция результатов/ошибки → повтор
    │       ├── peek_recent_chats → инъекция чатов → повтор
    │       ├── write/update_diary → инъекция результата → повтор
    │       └── иначе → reminder и следующий круг
    │
    └── yield "reply" / "silence" / "done" / "error"
```

## Состояния и протокол

Поведение зависит от `presence.state`:

| Состояние | Ответ | Инструменты |
|-----------|-------|-------------|
| `rest` | JSON-ответ невозможен; текст без тула игнорируется | `enter_dialog`, `send_message` (одна отмашка), `enter_working`, `start_sleep` |
| `working` | то же | `enter_dialog`, `send_message`, `enter_rest`, `start_sleep` |
| `dialog` | JSON `{"messages": [{"content": ...}]}` (1–3) или `{"messages": []}` | `search_memory`, `write_diary`, `update_diary`, presence-тулы, `start_sleep` |
| `sleep` | пайплайн обычно не работает | `enter_dialog`, `start_sleep` |
| proactive | `rest/working` + `peek_recent_chats` | те же + peek |

После `enter_dialog` пайплайн **пересобирает system prompt и tools** и вызывает
LLM снова — уже по правилам диалога. После `enter_rest`/`enter_working` — наоборот.

## Формат ответа в диалоге

```json
{"messages": [{"content": "первый пузырь"}, {"content": "второй пузырь"}]}
```

- максимум `MAX_BUBBLES = 3`; лишние отбрасываются;
- пустой массив — валидное молчание;
- код-фенсы и текст вокруг JSON вырезаются парсером;
- ведущий `[YYYY-MM-DD HH:MM:SS]`, скопированный из истории, удаляется;
- при поддержке провайдера запрашивается `response_format={"type":"json_schema", ...}`;
  если провайдер отверг `response_format`, один раз повторяется prompt-only.

## Стриминговые события

`process_message_stream()` — асинхронный генератор (его потребляет `core/queue.py`
и `core/proactive.py`):

```
thought       → {"type": "thought", "thoughts": "..."}
action        → {"type": "action", "action": {...}}
search_result → {"type": "search_result", "content": "..."}   (memory или peek)
reply         → {"type": "reply", "texts": [...], "text": "...", "chat_id": "...", "via": "bubbles"|"send_message_brush_off"}
silence       → {"type": "silence", "reason": "..."}
done          → {"type": "done", "result": {...}}
error         → {"type": "error", "error": "..."}
```

`process_message()` выполняет тот же цикл и возвращает `result` целиком.

## Результат

```python
{
  "success": True,
  "actions": [...],
  "thoughts": "...",            # legacy: равен reply_text
  "reply_text": "...",
  "reply_texts": ["..."],
  "reply_via": "bubbles" | "send_message_brush_off" | "",
  "reply_chat_id": "...",
  "silence_reason": "...",
  "raw_llm_responses": [...],
  "system_prompt": "",          # legacy-поле, не заполняется
  "llm_messages": [],           # legacy-поле, не заполняется
  "timing": {"total": "3.21s", "iterations": 2}
}
```

## Медиа

`_attach_media()`:

- текущий батч (фото/аудио) вставляется в **последнее** user-сообщение;
- последние `MAX_HISTORY_IMAGES = 3` фото и `MAX_HISTORY_AUDIOS = 2` аудио
  подтягиваются из `chunks.media_path` и вставляются **в свои** сообщения по
  плейсхолдерам `[фото...]` / `[голосовое...]`;
- если сообщение уже вышло из контекста — байты отбрасываются, а не
  приклеиваются к текущему ходу;
- аудио конвертируется в wav/16kHz mono через ffmpeg; при отсутствии ffmpeg
  нативный звук пропускается (плейсхолдер остаётся);
- при ошибке провайдера на аудио `LLMClient` делает один текстовый ретрай.

## Логирование

Каждый прогон пишется в `logs/pipeline_*.json` (`LOG_DIR`), хранится 200 последних
файлов. Основные поля:

```json
{
  "timestamp": "2026-10-29T12:00:00",
  "user_id": "...",
  "chat_id": "...",
  "user_message": "...",
  "system_prompt": "",
  "llm_messages": [],
  "thoughts": "...",
  "reply_text": "...",
  "reply_texts": ["..."],
  "reply_via": "bubbles",
  "reply_chat_id": "...",
  "silence_reason": "",
  "actions": [...],
  "raw_llm_responses": [
    {"iteration": 1, "thoughts": "...", "tool_calls": [...], "finish_reason": "tool_calls"}
  ],
  "success": true,
  "error": null,
  "timing": {"total": "3.21s", "iterations": 2}
}
```

Просмотр: `GET /api/pipeline/logs`, `/api/pipeline/logs/{file}`,
`GET /api/pipeline/chats`; UI — страница Pipeline. Детальный эндпоинт заменяет
мультимодальные content-блоки на метки `[фото]` / `[голосовое сообщение]`,
base64 в ответ API не попадает.

## Важные константы

| Константа | Значение | Где | Описание |
|-----------|----------|-----|----------|
| `MAX_THINK_ITERATIONS` | 7 | `core/pipeline.py` | Fallback-лимит, runtime может переопределить |
| `MAX_BUBBLES` | 3 | `core/pipeline.py` | Максимум пузырей в диалоге |
| `MAX_HISTORY_IMAGES` | 3 | `core/media.py` | Фото, остающиеся нативными в истории |
| `MAX_HISTORY_AUDIOS` | 2 | `core/media.py` | Аудио, остающиеся нативными в истории |
| `CONTEXT_MAX_TOKENS` | 30000 | `config.py` | Лимит токенов сообщений в буфере |
| `MAX_CONTEXT_TOKENS` | 10000 | `config.py` | Целевой лимит выборки контекста |
| `MAIN_LLM_MAX_TOKENS` | 2048 | `config.py` | Дефолт; пайплайн явно просит 2048 |

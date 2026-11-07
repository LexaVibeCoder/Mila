# Основной пайплайн (ThinkPipeline)

**Файл:** `core/pipeline.py`

`ThinkPipeline` — ядро рассуждения Милы. Это **direct-tools** цикл: LLM получает
реальные функции (`enter_dialog`, `send_message`, `search_memory`, ...), а не
обёртку `think()`. В диалоге ответ приходит обычным текстом: пузыри разделяются
пустой строкой, пустой ответ означает молчание.

Название `ThinkPipeline` сохранено для совместимости; алиас — `MilaPipeline`.

## Логика работы

```
process_message_stream() / process_message()
    │
    ├── req_user / req_chat — снимок хода (параллельные чаты не перетирают self.*)
    ├── journal_context = get_journal_context()   — снимок журнала на весь ход
    ├── messages = list(agent_internal_history)
    │   ├── proactive_block? → system-блок с инструкциями и списком чатов
    │   └── иначе → notification-блок для rest/working (последние 2000 символов)
    ├── _attach_media(): текущие фото/аудио + последние 3 фото и 2 аудио из истории
    │
    ├── while iteration < max_iterations (runtime, по умолчанию 7):
    │   ├── presence = снимок TurnContext / get_presence() (обновляется только этим ходом)
    │   ├── system_prompt = build_main_llm_system_prompt(
    │   │       ..., journal_context=journal_context)          (заново каждый раз)
    │   ├── tools = get_tool_definitions(presence_state, include_proactive=...)
    │   ├── LLM.generate(messages, system_prompt, tools, tool_choice="auto")
    │   │
    │   ├── нет tool_calls:
    │   │   ├── dialog → split_reply_bubbles(content)
    │   │   │   ├── пусто → silence
    │   │   │   └── абзацы через пустую строку → reply
    │   │   └── rest/working:
    │   │       ├── пусто → silence
    │   │       └── текст → нарушение протокола → reminder + ретрай
    │   │
    │   └── есть tool_calls:
    │       ├── контент этого хода НЕ отправляется (это рассуждение)
    │       ├── для каждого action: execute_action → apply_action_effect
    │       ├── start_sleep(sleeping) → silence (старт журналируется в sleep manager)
    │       ├── enter_*/switched → system-сообщение с `[событие]` в конце → повтор
    │       ├── send_message(ready) → кандидат на финальный ответ (журнал — после отправки)
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
| `rest` | текстовый ответ здесь игнорируется | `enter_dialog`, `send_message` (одна отмашка), `enter_working`, `start_sleep` |
| `working` | то же | `enter_dialog`, `send_message`, `enter_rest`, `start_sleep` |
| `dialog` | обычный текст; пузыри через пустую строку; пустой ответ = молчание | `search_memory`, `write_diary`, `update_diary`, presence-тулы, `start_sleep` |
| `sleep` | пайплайн обычно не работает | `enter_dialog`, `start_sleep` |
| proactive | `rest/working` + `peek_recent_chats` | те же + peek |

После `enter_dialog` пайплайн **пересобирает system prompt и tools** и вызывает
LLM снова — уже по правилам диалога. После `enter_rest`/`enter_working` — наоборот.
В уведомлении и блоке presence LLM видит постоянный алиас чата вместо ID;
executor разрешает алиас в ID до изменения presence или отправки. Неверный
алиас возвращается в контекст как ошибка и не приводит к отправке.

При смене состояния пайплайн добавляет в **самый конец** контекста свежее
событие журнала:

```
Presence switched. Decide in the new state now.
[событие] [2026-11-06 19:14:30] ты зашла в чат sokol (Федос) из rest, до этого была в rest 21 мин. заметка: ...
```

`journal_context` читается один раз в начале хода, поэтому это событие не
дублируется в system prompt. На следующем ходе оно уже находится в блоке
«Что ты делала сегодня». `send_message` пишется в журнал в
`core.delivery.deliver_messages()` после успешной отправки. `search_memory`,
`peek_recent_chats` и дневник в журнал не попадают. Подробности —
[action-journal.md](action-journal.md).

## Формат ответа в диалоге

Ответ — обычный текст. Пузыри разделяются пустой строкой (`\n\n`, пробелы
между переводами строк допускаются):

```
первый пузырь

второй пузырь
```

- `split_reply_bubbles()` режет ответ по пустым строкам, лимита на число
  пузырей нет;
- границей пузыря также считается `\n` перед строкой, начинающейся с
  `[YYYY-MM-DD HH:MM(:SS)]`: модель иногда помечает желаемый разрыв
  собственным timestamp вместо пустой строки;
- одиночный `\n` внутри абзаца сохраняется как есть;
- пустой или whitespace-only ответ — валидное молчание, ретрая нет;
- `[YYYY-MM-DD HH:MM(:SS)]` в начале пузыря (в т.ч. скопированный из
  истории) удаляется; timestamp в середине строки не трогается;
- `response_format` больше не запрашивается: провайдер получает обычный
  текстовый ответ.

## Стриминговые события

`process_message_stream()` — асинхронный генератор (его потребляет `core/queue.py`
и `core/proactive.py`):

```
thought       → {"type": "thought", "thoughts": "..."}
action        → {"type": "action", "action": {...}}
search_result → {"type": "search_result", "content": "..."}   (memory или peek)
reply         → {"type": "reply", "texts": [...], "text": "...", "via": "bubbles"|"send_message_brush_off"}
silence       → {"type": "silence", "reason": "..."}
done          → {"type": "done", "result": {...}}
error         → {"type": "error", "error": "..."}
```

Событие `reply` **не содержит получателя**: маршрут определяется `TurnContext`
(владелец хода или явно разрешённая цель), а не глобальным `presence`.
`_run_stream`/`process_message_stream` принимают `turn=TurnContext(...)`;
внутри `presence` снимается один раз на старте хода и перечитывается только
после собственного `enter_dialog/enter_rest/enter_working` этого же хода.

`process_message()` выполняет тот же цикл и возвращает `result` целиком.
`result["reply_chat_id"]` остаётся для логов/Observatory и вычисляется из
`turn.resolve_target()`, а не из события.

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
    {"iteration": 1, "thoughts": "...", "reasoning": "...", "tool_calls": [...], "finish_reason": "tool_calls"}
  ],
  "success": true,
  "error": null,
  "timing": {"total": "3.21s", "iterations": 2}
}
```

`raw_llm_responses[].reasoning` — «мысли» модели (`message.reasoning` у
OpenRouter, `reasoning_content` у локальных серверов), best-effort. Поле
появляется только если провайдер его вернул, то есть при
`reasoning_effort != "none"`. В UI на странице Pipeline оно показывается
отдельным блоком **Mila's reasoning** по итерациям.

Просмотр: `GET /api/pipeline/logs`, `/api/pipeline/logs/{file}`,
`GET /api/pipeline/chats`; UI — страница Pipeline. Детальный эндпоинт заменяет
мультимодальные content-блоки на метки `[фото]` / `[голосовое сообщение]`,
base64 в ответ API не попадает.

## Важные константы

| Константа | Значение | Где | Описание |
|-----------|----------|-----|----------|
| `MAX_THINK_ITERATIONS` | 7 | `core/pipeline.py` | Fallback-лимит, runtime может переопределить |
| `MAX_HISTORY_IMAGES` | 3 | `core/media.py` | Фото, остающиеся нативными в истории |
| `MAX_HISTORY_AUDIOS` | 2 | `core/media.py` | Аудио, остающиеся нативными в истории |
| `JOURNAL_MAX_LINES` | 60 | `core/action_journal.py` | Сколько строк журнала попадает в промпт |
| `SEND_TEXT_LIMIT` | 200 | `core/action_journal.py` | Максимум текста `send_message` в журнале |
| `CONTEXT_MAX_TOKENS` | 30000 | `config.py` | Лимит токенов сообщений в буфере |
| `MAX_CONTEXT_TOKENS` | 10000 | `config.py` | Целевой лимит выборки контекста |
| `MAIN_LLM_MAX_TOKENS` | 2048 | `config.py` | Дефолт; пайплайн явно просит 2048 |

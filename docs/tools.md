# Система тулзов (Tools)

**Файл:** `core/tools/executor.py`

LLM вызывает **настоящие функции** (direct tools). Набор зависит от текущего
`presence`-состояния, поэтому Мила видит только то, что уместно сейчас.

```python
get_tool_definitions(presence_state="rest", include_proactive=False) -> list[dict]
```

`silence` остался в runtime-конфиге для совместимости, но **не входит** в
definitions: молчание = пустой ответ.

## Доступные действия

| Действие | Состояния | Параметры | Что делает |
|----------|-----------|-----------|------------|
| `enter_dialog` | rest, working, dialog | `chat_alias!`, `note!` | Переключает presence в диалог; пайплайн перевызывает LLM |
| `enter_rest` | working, dialog | `note?` | Возврат в rest |
| `enter_working` | rest, dialog | `note?` | Переход в working |
| `start_sleep` | все | `duration_minutes!` (1–720), `note?` | Запускает ночной цикл |
| `send_message` | rest, working, dialog | `text!`, `chat_alias?` (в dialog обязателен) | Одна короткая «отмашка», не диалог |
| `search_memory` | dialog | `query!` | FAISS top-10 → rerank → top-5 |
| `write_diary` | dialog | `entry!`, `category?` | Новая запись дневника |
| `update_diary` | dialog | `entry!`, `entry_id!`, `category?` | Обновление записи |
| `peek_recent_chats` | proactive (rest/working) | `limit_per_chat?`, `chat_aliases?` | Последние сообщения по всем чатам |

> В диалоге обычный ответ — текст, а `send_message` позволяет отмахнуться от
> уведомления другого чата без переключения. В состоянии `sleep` доступны `enter_dialog` и `start_sleep`
> (пайплайн там почти не работает).

## Динамический набор

`get_tool_definitions()` читает `runtime_config.get("tools")`:

```json
{
  "search_memory": true,
  "send_message": true,
  "silence": true,
  "write_diary": true,
  "update_diary": true,
  "presence": true,
  "peek_recent": true
}
```

- `presence` выключает все четыре presence-тула разом (`enter_dialog`,
  `enter_rest`, `enter_working`, `start_sleep`);
- `peek_recent` действует только при `include_proactive=True` (проактивный ход);
- отключённые тулзы не попадают в definitions и недоступны модели.

## Поведение executor-а

### `enter_dialog`

```json
{"action_type": "enter_dialog", "params": {"chat_alias": "алекс", "note": "хочу поговорить"}}
```

- `chat_alias` и непустой `note` обязательны; executor преобразует алиас во внутренний `chat_id`;
- имя для presence берётся из `chats.chat_name`;
- возвращает `{type, chat_id, user_name, state: "dialog", presence, status: "switched", journal_event}`;
- пайплайн инжектит system-сообщение со свежим событием в конце и вызывает LLM
  уже в диалоге.

### `enter_rest` / `enter_working`

Меняют `presence_state`, возвращают `status: "switched"`. После этого набор
тулзов и системный промпт пересобираются.

### `start_sleep`

- `duration_minutes` обязателен, клиппится в `1..720`;
- `sleep.manager.start_sleep(triggered_by="mila_tool", note=...)` сам переводит
  presence в `sleep` и пишет старт в журнал с реальным предыдущим состоянием;
- если сон отклонён (например, `forced_awake`), presence не меняется;
- успех → `status: "sleeping"`, пайплайн завершается как при silence.

### `send_message`

Не отправляет сообщение сам. Возвращает:

```json
{"type": "send_message", "text": "...", "chat_id": "...", "status": "ready"}
```

Это финальная короткая отмашка: пайплайн отдаёт `reply_tool`,
`queue.py` отправляет один пузырь, после успешной отправки
`deliver_messages()` пишет строку в глобальный журнал. В диалоге схема требует алиас.
Для ответа на уведомление `chat_alias` можно опустить — используется текущий
чат. В проактивном ходе алиас обязателен. Явно указанный неизвестный или пустой
алиас никогда не заменяется текущим чатом: инструмент возвращает ошибку, а LLM
получает возможность исправить вызов. Аналогично для `enter_dialog`.

Алиас можно писать с `@`, в другом регистре или с пробелами вместо дефисов.
Для совместимости executor принимает прежний параметр `chat_id` и числовой ID,
но только для уже существующего чата; в схемах инструментов модели показаны
только алиасы. Подробнее о постоянных алиасах и миграции — [database.md](database.md#chat_aliases).

### `search_memory`

1. Эмбеддинг запроса (`EmbeddingClient`).
2. `VectorMemory.search(k=10)`.
3. Загрузка `memories` по `faiss_id` **с фильтром `user_id`** — чужие факты не
   попадают в промпт. Legacy-записи с `NULL user_id` доступны только если
   per-user совпадений нет.
4. `RerankerClient.rerank(query, documents)` с порогом `0.6`.
5. Топ-5 новых строк → `context_addon`; статус `searched` или `error`.

В рамках одного хода все вызовы `search_memory` делят один набор уже
показанных фактов (`seen_memories`): совпадения по `mem_id` или нормализованному
тексту отбрасываются. Если запрос целиком повторяет ранее найденное, в контекст
уходит `[No new facts: ...]` вместо повторной вставки тех же строк — модель часто
делает несколько пересекающихся поисков за одну итерацию.

Reranker опционален. Если модель не установлена или не загрузилась, executor
ловит ошибку и использует порядок FAISS; `search_memory` продолжает работать,
но ранжирование хуже. Установить модель: `install_reranker.py`.

### `write_diary` / `update_diary`

- текст принимается как `entry`, fallback — `text`;
- `category` нормализуется в `day_entries` / `reflection_entries`;
- `update_diary` требует `entry_id` из блока дневника `[ID: N]`;
- статусы: `written`, `updated`, `not_found`, `error`.

### `peek_recent_chats`

Только для проактивного хода:

- возвращает чаты от старых к свежим, свежие внизу;
- `limit_per_chat` по умолчанию `proactive.default_peek_limit` (10), клиппится
  в `1..50`;
- `chat_aliases` опционально ограничивает список; неизвестный алиас отклоняет
  весь запрос, вместо подстановки другого чата;
- текст сообщений **не обрезается**;
- статус `peeked` → `context_addon` инжектится в контекст, LLM выбирает один чат.

## После каждого действия

Пайплайн вызывает `NeedsTracker.apply_action_effect(action_type)`:

| Действие | Эффект |
|----------|--------|
| `send_message` | social −0.15 |
| `enter_dialog` | social −0.1 |
| `silence` / `enter_rest` | fatigue −0.05 |
| `start_sleep` | fatigue −0.1 |
| `search_memory` / `peek_recent_chats` | boredom −0.1 |
| `write_diary` | boredom −0.08, fatigue +0.03 |
| `update_diary` | boredom −0.04 |

## Журнал действий

В глобальный журнал (`core/action_journal.py`) попадают только действия,
которые меняют место/состояние или влияют на внешний мир:

- `enter_dialog` / `enter_rest` / `enter_working` — с временем, прошлым
  состоянием и его длительностью (у `enter_dialog`);
- `start_sleep` и пробуждение — с реальной длительностью сна;
- `send_message` — после фактической отправки, текст до 200 символов.

`search_memory`, `write_diary`/`update_diary`, `peek_recent_chats` и обычные
ответы в диалоге в журнал не пишутся. Подробности —
[action-journal.md](action-journal.md).

## Управление через API

`GET /api/tools` — список 7 ключей с описаниями.
`PUT /api/tools/{tool_name}`:

```json
{ "enabled": false }
```

Допустимые имена: `search_memory`, `send_message`, `silence`, `write_diary`,
`update_diary`, `presence`, `peek_recent`. Изменения применяются со следующего
вызова `get_tool_definitions()`. UI — страница Tools.

## Тулзы сна

В `core/sleep/tools.py` — отдельные наборы, недоступные в основном пайплайне:

- `add_memory_chunk`, `update_memory_chunk`, `delete_memory_chunk`;
- `save_dialog_summary`, `skip_dialog`;
- `add_user_fact`, `skip_user_fact_extraction`;
- `approve_changes`, `reject_changes`.

Подробности — [sleep-system.md](sleep-system.md).

## Пайплайн-интеграция

```
LLM → tool_calls
        │
        ├── enter_dialog/rest/working → presence switch → перевызов LLM
        ├── send_message (rest/working) → reply_tool → отправка одного пузыря
        ├── search_memory / peek_recent_chats → контекст → перевызов LLM
        ├── write/update_diary → подтверждение → перевызов LLM
        ├── start_sleep → ночной цикл → завершение как silence
        └── нет тулзов в dialog → текст, пузыри через пустую строку → reply
```

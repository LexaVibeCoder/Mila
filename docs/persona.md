# Персона и системный промпт

**Файл**: `core/llm/persona.py`

Сборка системного промпта для LLM. **Текст промптов живёт не в коде, а в Markdown-файлах `core/llm/prompts/*.md`** (23 файла) — `persona.py` только загружает их (`_load_prompt`, статика кэшируется через `lru_cache`) и склеивает с runtime-контекстом.

## Фрагменты (core/llm/prompts/)

**Статика** (порядок в `_STATIC_PARTS`, склейка через `\n\n---\n\n`):

| Файл | Роль |
|------|------|
| `MILA_PREFERENCES.md` | Базовый голос |
| `MILA_BIO.md` | Биография (со своим заголовком) |
| `IDENTITY.md` | Стабильное ядро личности |
| `CLOSENESS_TIERS.md` | Дистанции + примеры |
| `VOICE_MECHANICS.md` | Механика голоса |
| `BANNED_AI_ISMS.md` | Запрещённые AI-штампы |
| `MEMORY_HONESTY.md` | Честность памяти |
| `SILENT_MODE.md` | Молчание в истории |
| `STYLE_STABILITY.md` | Стабильность стиля |
| `NO_ROLEPLAY.md` | Без отыгрыша ролей |
| `DIARY_GUIDE.md` | Инструкция дневника (статика) |

**Динамические шаблоны** (плейсхолдеры `{...}` заполняются в рантайме):

| Файл | Плейсхолдеры |
|------|--------------|
| `HEADER.md` | `{now}`, `{user_name}`, `{chat_id}` |
| `RELATIONSHIP_BLOCK.md` | `{relationship}` |
| `RELATIONSHIP_FRIEND.md` / `RELATIONSHIP_ACQUAINTANCE_WARM.md` / `RELATIONSHIP_NEUTRAL.md` / `RELATIONSHIP_ACQUAINTANCE_COLD.md` / `RELATIONSHIP_STRANGER_COLD.md` | `{user_name}` — выбор по sentiment (см. ниже) |
| `NEEDS_BLOCK.md` | `{needs_summary}` |
| `DIARY_BLOCK.md` | `{diary_context}` (только если дневник не пуст) |
| `SESSION_BLOCK.md` + `SESSION_TIME.md` | `{session_time}` ← `User responded after {formatted}` (только если пауза ≥ 30с) |
| `FUNCTION_CALLING.md` | `{chat_id}` — протокол `think` (send/search/silence/diary + enter_dialog/enter_rest/enter_working/start_sleep) |

Чтобы поменять характер Милы — правьте `.md` файлы (перезапуск не нужен для динамики, статика кэширована в процессе бота).

## Компоненты системного промпта

```
build_main_llm_system_prompt(user_id, chat_id, my_user_id)
    │
    ├── 1. Статический префикс (lru_cache, 11 блоков через ---)
    │       └── НО: если runtime persona.custom_text непуст —
    │           статика ЦЕЛИКОМ заменяется им
    │
    ├── 2. HEADER {now, user_name, chat_id}
    ├── 3. RELATIONSHIP_BLOCK
    │       └── _get_profile_context(user_id): users.preferred_name/user_name,
    │           sentiment → выбор варианта:
    │           >0.6 friend, >0.2 acquaintance_warm, <-0.6 stranger_cold,
    │           <-0.2 acquaintance_cold, иначе neutral
    ├── 4. NEEDS_BLOCK ← NeedsTracker.get_needs_summary()
    │       (fatigue/social/boredom high/medium/low + подсказки)
    ├── 4b. PRESENCE-блок ← core/presence.format_state_block()
    │       (rest/dialog+focus/working/sleep + кто пишет: имена, не id;
    │       собирается в коде, не .md-файл; при чужом уведомлении в диалоге —
    │       явная пометка + напоминание про enter_dialog/silence)
    ├── 5. DIARY_BLOCK ← get_today_diary_context() (до 30 записей,
    │       группировка day/reflection, каждая с [ID: N] для update_diary)
    ├── 6. SESSION_BLOCK ← пауза с последнего ответа Милы (чанк от my_user_id
    │       с time_start; пропуск, если < 30с)
    └── 7. FUNCTION_CALLING ← протокол think + chat_id
```

Финальный порядок частей: `[header, static|custom, relationship, needs, presence, diary?, session?, function_calling]`.

## Что в документации раньше было неверно

- **Автобиографическая память (`get_all_chunks_markdown()`) в system prompt НЕ инжектится.** `persona.py` её не вызывает — чанки `mila_memory_chunks` используются только ночным `process_diary_analysis`. В дневной генерации у модели есть дневник + `search_memory`, но не вечная память целиком.
- **Поля `persona.custom_prompt` не существует.** Реальное поле — `persona.custom_text`, и оно не «добавляется», а **полностью заменяет** статические 11 блоков.
- **Слайдеры `persona.parameters` (empathy/humor/formality/creativity) сейчас ни на что не влияют** — в `persona.py` нет кода, который вставлял бы их в промпт. Значения хранятся и редактируются через API, применение — задача будущих изменений.
- Биография/предпочтения — это тексты `MILA_BIO.md` / `MILA_PREFERENCES.md`, а не константы в коде; точное содержимое смотрите в этих файлах.

## _get_session_time_info()

Ищет в последних 50 чанках чата последний ответ самой Милы (`user_id == my_user_id` с `time_start`), считает дельту от `now (UTC)`. Формат `_format_time_delta`: `"{d} days {h} hours {m} minutes"` / `"{h} hours {m} minutes"` / `"{m} minutes"`. Дельта < 30 секунд → блок опускается.

## Редактирование через API

### Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/persona` | Текущие настройки |
| PUT | `/api/persona` | Обновление (`custom_text`, `parameters.*`) |
| POST | `/api/persona/reset` | Сброс на defaults |

### Пример запроса

```json
PUT /api/persona
{
  "custom_text": "Ты — Мила. Ты сегодня особенно задумчива...",
  "parameters": { "empathy": 0.8, "humor": 0.3, "formality": 0.4, "creativity": 0.9 }
}
```

Пустой `custom_text` (`""`) = вернуться к сборке из `.md` файлов.

## Интеграция с пайплайном

System prompt собирается при каждом запуске `process_message()` / `process_message_stream()`, так как меняются время, `needs_summary`, дневник и `custom_text`. Статика кэширована (`lru_cache`), динамика — свежая каждый раз.

## Пример структуры финального system prompt

```
[HEADER: кто ты, дата/время now, с кем говоришь user_name, chat_id]
---
[MILA_PREFERENCES]
---
[MILA_BIO]
---
[IDENTITY / CLOSENESS_TIERS / VOICE_MECHANICS / ...]
---
[RELATIONSHIP_BLOCK: строка отношений по sentiment]
---
[NEEDS_BLOCK: fatigue/social/boredom + подсказки]
---
[DIARY_BLOCK: записи за сегодня с [ID: N]]            (если есть)
---
[SESSION_BLOCK: User responded after ...]             (если пауза ≥ 30с)
---
[FUNCTION_CALLING: протокол think, chat_id]
```

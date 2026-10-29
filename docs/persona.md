# Персона и системный промпт

**Файл:** `core/llm/persona.py`

System prompt собирается из Markdown-фрагментов `core/llm/prompts/` и
runtime-контекста. Текст персоны живёт **только** в `.md`-файлах; Python-код
загружает и склеивает их.

## Структура промптов

```
core/llm/prompts/
├── persona/        # кто Мила есть — статика, всегда включена
│   ├── prefs.md
│   ├── bio.md
│   ├── identity.md
│   ├── closeness.md
│   ├── voice.md
│   ├── banned.md
│   ├── memory.md
│   ├── style.md
│   └── no_roleplay.md
├── diary/
│   └── guide.md    # инструкция дневника (статика)
├── relationship/   # строка отношений под текущего пользователя
│   ├── block.md
│   ├── friend.md
│   ├── acq_warm.md
│   ├── acq_cold.md
│   ├── neutral.md
│   └── stranger.md
├── context/        # динамические блоки
│   ├── header.md
│   ├── history_note.md
│   ├── media_note.md
│   ├── needs.md
│   ├── diary_block.md
│   ├── session.md
│   └── session_time.md
└── states/         # ровно один файл под текущий presence
    ├── rest.md
    ├── dialog.md
    └── working.md
```

`_load(name)` принимает путь без расширения (`persona/voice`). Для старых плоских
имён есть `LEGACY_MAP` (`MILA_BIO` → `persona/bio`, `HEADER` → `context/header`
и т.д.).

## Статический префикс

`_STATIC_PERSONA` (порядок важен):

1. `persona/prefs`
2. `persona/bio`
3. `persona/identity`
4. `persona/closeness`
5. `persona/voice`
6. `persona/banned`
7. `persona/memory`
8. `persona/style`
9. `persona/no_roleplay`
10. `diary/guide`

Склеиваются через `\n\n---\n\n`, кэшируются `lru_cache(maxsize=1)`.

Если `runtime_config["persona.custom_text"]` непуст — статический префикс
**полностью заменяется** этим текстом (а не дополняется).

## Порядок сборки

```
build_main_llm_system_prompt(user_id, chat_id, my_user_id)
    │
    ├── 1. static_prefix (или custom_text)
    ├── 2. context/history_note      — о двойных сообщениях и паузах
    ├── 3. context/media_note        — как смотреть фото и слушать голосовые
    ├── 4. relationship/block        — выбранная строка отношений
    ├── 5. format_state_block()      — где Мила и кто пишет (код, не .md)
    ├── 6. states/{rest|dialog|working}.md — поведение ровно для текущего состояния
    ├── 7. context/diary_block       — только если дневник не пуст
    ├── 8. context/needs             — fatigue/social/boredom + подсказки
    ├── 9. context/header           — «Ты Мила...», время, чат, имя
    └── 10. context/session          — только если пауза ≥ 30с
```

Финал: `"\n\n".join(parts).strip()`.

## Выбор отношений

`_get_profile_context(user_id)`:

- имя: `preferred_name` → `user_name` → `"user"`;
- sentiment → шаблон:

| Sentiment | Файл | Tier |
|-----------|------|------|
| `> 0.6` | `relationship/friend` | ДРУГ |
| `> 0.2` | `relationship/acq_warm` | тёплый знакомый |
| `< -0.6` | `relationship/stranger` | чужой/конфликт |
| `< -0.2` | `relationship/acq_cold` | знакомый с холодком |
| иначе | `relationship/neutral` | знакомый/чужой по истории |

Плейсхолдер `{user_name}` подставляется в выбранный шаблон.

## Состояния (`states/`)

Модель видит **только один** файл состояния:

- `rest.md` — три варианта: `enter_dialog`, разовый `send_message`, промолчать;
- `dialog.md` — формат JSON `{"messages": [...]}`, 1–3 пузыря, `[]` = молчание;
- `working.md` — как rest, но с «занята».

Неизвестное состояние падает в `rest`. Блок `format_state_block()` только
описывает локацию и собеседника; правила поведения — в `states/*.md`.

## Динамические блоки

### `context/header.md`

Плейсхолдеры: `{now}`, `{user_name}`, `{chat_id}`.

### `context/needs.md`

`{needs_summary}` ← `NeedsTracker.get_needs_summary()`:

```
• Fatigue: high (0.82)
• Need for Social Interaction: medium (0.55)
• Boredom: low (0.21)
```

Плюс подсказки при значениях > 0.7. Состояние влияет на тон и длину ответа, но
не перечисляется в самом ответе.

### `context/diary_block.md`

`{diary_context}` ← `get_today_diary_context()` (до 30 записей, группировка
`day_entries` / `reflection_entries`, у каждой `[ID: N]`).

### `context/session.md` + `session_time.md`

`_get_session_time_info(chat_id, my_user_id)`:

- берёт последние 50 `chunks` чата;
- ищет последнее сообщение самой Милы (`user_id == my_user_id`);
- считает дельту от `now (UTC)`;
- < 30 секунд → блок опускается;
- формат: `"N days H hours M minutes"` / `"H hours M minutes"` / `"M minutes"`.

## Что важно помнить

- **Автобиографическая память (`mila_memory_chunks`) в дневной prompt не
  инжектится.** Её читает/меняет только ночная рефлексия.
- **Поля `persona.custom_prompt` нет.** Реальное — `persona.custom_text`.
- **Слайдеры `persona.parameters` (empathy/humor/formality/creativity) не влияют
  на промпт** — они хранятся и редактируются, но код их не подставляет.
- **`FUNCTION_CALLING.md` больше нет**: протокол вызова тулзов описан в
  `states/*.md` и в definitions инструментов.
- Изменения `.md` подхватываются без рестарта для динамических блоков; статика
  кэширована в процессе (`lru_cache`) — после правки статики нужен рестарт.

## Редактирование через API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/persona` | `custom_text`, `parameters`, `using_custom` |
| PUT | `/api/persona` | Обновление `custom_text` / `parameters.*` |
| POST | `/api/persona/reset` | Сброс `custom_text` в `""` |

```json
PUT /api/persona
{
  "custom_text": "Ты — Мила. ...",
  "parameters": {"empathy": 0.8, "humor": 0.3, "formality": 0.4, "creativity": 0.9}
}
```

Пустой `custom_text` = вернуться к сборке из `.md`-файлов.

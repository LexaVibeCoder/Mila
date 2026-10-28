# Сообщения: уведомления, presence и typing (Messaging Layer)

Покрывает модули: `core/events.py`, `core/presence.py`, `core/media.py`, `core/queue.py`, `core/bot.py`, `core/tg_adapter.py`, `core/tg_auth.py`.

## Обзор

```
Telegram (TDLib update) → bot.py → queue.py MessageCoordinate (per-chat воркер)
    → pipeline → queue (отправка) → tg_adapter (send_text/send_typing)
```

Каждое входящее личное сообщение — это **уведомление** (`core/events.py: Notification`), а не приказ ответить. Мила видит уведомление (состояние presence + последние сообщения) и сама решает: ответить, сменить состояние или промолчать. Принимаются текст, фото, войс; остальное (видео, стикеры, файлы) хранится заглушками-плейсхолдерами.

## MilaUserbot (`core/bot.py`)

- Клиент `pytdbot.Client` (`user_bot=True`, `files_directory=TD_FILES_DIRECTORY`, шифрование `TD_DATABASE_ENCRYPTION_KEY`, `td_verbosity=TD_VERBOSITY`).
- `register_auth_handlers(app, phone_hint)` — интерактивный логин (см. ниже).
- Хендлер сообщений: пропуск `is_outgoing` (защита от self-loops) → `is_private_chat()` → `handle_text_message()` (тонкий делегат в `messages.handle()`). Текстового фильтра больше нет: классификация (`text/photo/voice/video/sticker/...`) происходит в `queue.handle()` через `tg_adapter.normalize_event()` — ничего не дропается.
- Хендлер печати: `on_updateChatAction` (fallback `add_handler("updateChatAction")`) → `messages.handle_typing_update()`, свои экшены игнорируются по `my_id`.
- `start()`: `recover_from_crash()` → `app.start(wait_login=False)` → ожидание `AuthorizationStateReady` до 300с → `getMe()` → создание `ThinkPipeline(user_id="system", chat_id="system", my_user_id)` → `periodic_save()` (FAISS каждые `FAISS_SAVE_INTERVAL_MIN`) → `start_auto_sleep_scheduler()`.
- `_compress_context()` / `_build_histories()` — сжатие и сборка истории из контекстного буфера (см. [context-buffer.md](context-buffer.md)).
- `_resolve_roles_in_histories()` — pass-through (роли уже в буфере).

## MessageCoordinate (`core/queue.py`)

Per-chat воркер: один `asyncio.Task` на чат. `handle()` вызывается на каждое входящее и только **сохраняет + будит** воркера; вся логика — в воркере.

### handle(): уведомление → сохранение

```
normalize_event → ensure_user/chat → download_notification_media (photo/voice)
→ спит? queue_incoming_message() и выход
→ add_chunk(kind, media_path) + log_media()
→ push_message(chat_id, placeholder, kind)  [role="user"]
→ _pending[chat_id] += placeholder → _chat_user[chat_id] = user_id
→ encode для LLM (image_url / input_audio) в _batch_media
→ event.set() → ensure worker task
```

Каноническое хранилище (`chunks`) пишется немедленно, до генерации — сообщения не теряются. В `chunks`/`context_buffer` лежит текстовый плейсхолдер (`[фото: ...]`, `[голосовое сообщение]`), нативные байты идут в LLM только для текущего события (`ThinkPipeline._attach_media`). Отправитель чата запоминается в `_chat_user` — пайплайн пинится на реальный `user_id` (раньше оставался `"system"` и Мила не видела имени).

### Воркер: wake-on-every → typing-wait → generate

```
loop:
  1. _debounce(): wake-on-every — возврата на следующем тике, без окна
     тишины (MESSAGE_DEBOUNCE_SECONDS игнорируется; решение принимает сама Мила)
  2. typing-wait (если ENABLE_TYPING_WAIT):
     печатает → ждать сообщения / конца печати + grace / max wait;
     пришло сообщение → continue
  3. _generate_for_chat(): генерация; restart=True → continue
  4. _pending пуст → конец сессии батча (task завершается до следующего сообщения)
```

### Typing-поддержка (TDLib updateChatAction)

Состояние: `_typing_last[chat_id] = monotonic()` на каждый typing-like экшен; `ChatActionCancel` → снять. `is_user_typing()` — свежесть `TYPING_FRESHNESS_SECONDS` (6.0с; TDLib перестаёт слать typing ~через 6с).

- `_wait_typing()`: fast path (не печатает → 0 задержки) → иначе цикл по `TYPING_POLL_INTERVAL` (0.5с) до `TYPING_WAIT_MAX_SECONDS` (8.0с): сообщение пришло → выход (re-debounce); печать кончилась → grace `TYPING_GRACE_SECONDS` (2.0с) на доставку сообщения; max → «проceeding anyway».
- `_should_abort_for_typing()` = `ENABLE_TYPING_WAIT && TYPING_ABORT_GENERATION && is_user_typing()`.
- `_interrupted()` = `message | typing | None`. Проверяется: перед drain батча, на каждой итерации стрима, **перед каждой отправкой** и после генерации (fallback блокируется при interruption → restart).

### _generate_for_chat()

1. Pre-check interruption (typing → restart; message → continue).
2. Drain `_pending` → `batch_text` (`"\n".join`); пусто → выход. Забрать `_batch_media` (иначе `None`).
3. `_compress_context(chat_id)`.
4. **Needs pre-filter**: `fatigue > 0.85` и ночь (22–7ч) → auto-silence без LLM (`add_chunk(content="", llm_full_response="[auto-silence...]")` + `apply_action_effect("silence")`).
5. Без typing-индикатора во время решения — осознанно: уведомление не приказ, Мила может промолчать. `send_typing` вызывается один раз только перед фактической отправкой (см. ниже), чтобы в Telegram не светилось «печатает…», пока она лишь выбирает.
6. `_build_histories` → pin `pipeline.user_id/chat_id` на реального отправителя → `process_message_stream(..., media=media)`:
   - `send_message` (непустой): pre-send interruption-check → `send_typing(target)` → пауза 0.3–1.0с → `send_text` → `add_chunk` + `push_message(role="assistant")`.
   - `silence` → флаг; `enter_dialog/enter_rest/enter_working` → лог (состояние уже переключено экзекутором, пайплайн перевызывает LLM); `start_sleep` → флаг тишины; `search_memory`/`write_diary`/`update_diary` → лог (исполнение уже в экзекуторе).
   - `done.thoughts` → `full_response` для fallback.
7. Post: interruption → restart; иначе fallback — `thoughts` > 80 символов → `send_typing` + автоотправка (пауза 0.5–2с), иначе сохранение молчаливого ответа (`content=""`).

## tg_adapter (`core/tg_adapter.py`) + events/media

Вся pytdbot-специфика в одном месте, остальной код работает с plain-типами:

- `NormalizedMessage{chat_id, user_id, text, message_id, sender_name, chat_name}` — все ID строками (legacy, для текста).
- `Notification` (`core/events.py`): `{kind, chat_id, user_id, text/caption, message_id, sender_name, chat_name, file_id, local_path, extra_hint}` + `placeholder_text()` / `llm_event_line()`.
- `classify_message_content()` — case-insensitive разбор (`messageText/messagePhoto/messageVoiceNote/...`, работает и с объектами pytdbot, и с dict-`@type`); фото выбирает biggest size, войс достаёт `file_id`, стикер — emoji + set name.
- `normalize_event()` — строит Notification для любого приватного сообщения, никогда не дропает и не бросает.
- `download_notification_media()` — `downloadFile(synchronous=True)` для photo/voice, лимит `MAX_MEDIA_MB`; путь пишется в `chunks.media_path` + `media_logs`.
- `core/media.py` — `encode_image_for_llm()` (ресайз до 1568px, JPEG data_url), `encode_audio_for_llm()` (base64 + формат), `build_multimodal_parts()` (OpenAI content blocks).
- `is_text_message()` — только `MessageText` (остался для совместимости, в hot path не используется).
- `is_private_chat()` — только `ChatTypePrivate`; групповые чаты бот игнорирует.
- `resolve_sender_name/resolve_chat_name` (для лички — имя собеседника), `normalize_message`, `send_text`, `send_typing`, `parse_chat_action_update`, `is_typing_like_action / is_cancel_action`.

## Presence (`core/presence.py`)

Явные состояния Милы (одна строка `presence_state id=1` в `mila.db`, переживает рестарты): `rest` (дефолт) / `dialog(focus_chat_id + focus_user_name)` / `working` (v1 — видимость как у rest) / `sleep` (намерение; сам цикл — у sleep manager). Переключаются только тулами (`enter_dialog/enter_rest/enter_working/start_sleep`, см. [tools.md](tools.md)). `format_state_block()` — блок в system prompt: где Мила и кто пишет (имена, не id).

## tg_auth (`core/tg_auth.py`) и create_session

- `register_auth_handlers(client, phone_hint)`: `WaitPhoneNumber` (hint из `PHONE_NUMBER` или ввод) → `WaitCode` → `WaitPassword` (2FA) → `Ready`. Дополнительно обрабатываются `WaitEmailAddress` / `WaitEmailCode` / `WaitRegistration` / `WaitPremiumPurchase` / `WaitOtherDeviceConfirmation`; неизвестные состояния печатаются как `[auth] Состояние ...`. Ввод читается с fd 0 в daemon-потоке (не блокирует выход по таймауту). В Docker без TTY — предупреждение `_warn_no_tty()` (используйте `docker attach`).
- `create_session.py` — one-shot: клиент TDLib → ожидание `AuthorizationStateReady` 300с → `getMe()` → печать пути `TD_FILES_DIRECTORY`. Обёртки: `start/create_session.sh` (venv через python3.11/uv + pip) и `start/create_session.bat`.

### Код не приходит

- Способ доставки и длина кода печатаются из `code_info`. `authenticationCodeTypeTelegramMessage` — код приходит сообщением в сервисный чат **«Telegram» (777000)** в уже авторизованном приложении, а не по SMS. Если `next_type` пуст, резервной доставки (SMS/звонок) Telegram для этого входа не предлагает.
- Таймаут 300с **не отправляет новый код**: TDLib восстанавливает `WaitCode` из `data/tdlib`. Повторный запуск скрипта покажет предупреждение «новый код НЕ отправлялся» — нужно ввести уже полученный код либо удалить `data/tdlib` и запросить код заново.
- Слишком частые запросы кода Telegram временно блокирует; после нескольких неудачных попыток нужно подождать несколько часов.

## Настройки (config.py)

| Параметр | По умолч. | Описание |
|----------|-----------|----------|
| `MESSAGE_DEBOUNCE_SECONDS` | `2.0` | Legacy: игнорируется (wake-on-every, решение принимает Мила) |
| `ENABLE_TYPING_WAIT` | `true` | Мастер-выключатель typing-логики |
| `TYPING_WAIT_MAX_SECONDS` | `8.0` | Верхняя граница ожидания печати |
| `TYPING_FRESHNESS_SECONDS` | `6.0` | Сколько typing-событие считается свежим |
| `TYPING_POLL_INTERVAL` | `0.5` | Шаг опроса в ожидании |
| `TYPING_GRACE_SECONDS` | `2.0` | Ожидание сообщения после конца печати |
| `TYPING_ABORT_GENERATION` | `true` | Отменять генерацию/отправку при печати |

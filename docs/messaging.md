# Сообщения: уведомления, presence и typing (Messaging Layer)

Покрывает модули: `core/events.py`, `core/presence.py`, `core/media.py`,
`core/queue.py`, `core/bot.py`, `core/tg_adapter.py`, `core/tg_auth.py`.

## Обзор

```
Telegram (TDLib update) → bot.py → queue.py MessageCoordinate (per-chat worker)
    → pipeline → queue (reply bubbles) → tg_adapter (send_text/send_typing)
```

Каждое входящее личное сообщение — это **уведомление** (`core/events.py: Notification`),
а не приказ ответить. Мила видит состояние presence, последние сообщения и само
уведомление, после чего решает: войти в диалог и ответить, отправить одну
короткую отписку, сменить состояние или промолчать.

Принимаются текст, фото и войс нативно; остальное (видео, стикеры, файлы,
гифки, аудио) сохраняется плейсхолдерами.

## MilaUserbot (`core/bot.py`)

- Клиент `pytdbot.Client` (`user_bot=True`, `files_directory=TD_FILES_DIRECTORY`,
  шифрование `TD_DATABASE_ENCRYPTION_KEY`, `td_verbosity=TD_VERBOSITY`).
- `register_auth_handlers(app, phone_hint)` — интерактивный логин.
- Хендлер сообщений: пропуск `is_outgoing` → `is_private_chat()` (с одним
  ретраем при transient-ошибке) → `messages.handle()`. Текстового фильтра нет:
  классификация происходит в `queue.handle()` через `tg_adapter.normalize_event()`,
  ничего не дропается.
- Хендлер печати: `on_updateChatAction` (fallback `add_handler("updateChatAction")`)
  → `messages.handle_typing_update()`, свои экшены игнорируются по `my_id`.
- `start()`: `recover_from_crash()` → `app.start(wait_login=False)` → ожидание
  `AuthorizationStateReady` до 300с → `getMe()` → `ThinkPipeline(user_id="system",
  chat_id="system", my_user_id)` → `periodic_save()` (FAISS) → heartbeat-файл →
  `start_auto_sleep_scheduler()` → `start_auto_rest_scheduler()` →
  `start_proactive_scheduler()`.
- `_compress_context()` / `_build_histories()` — сжатие и сборка истории из
  контекстного буфера (см. [context-buffer.md](context-buffer.md)).
- `_resolve_roles_in_histories()` — pass-through (роли уже в буфере).
- `stop()` отменяет периодические задачи, per-chat воркеры, планировщики и
  сохраняет FAISS. Heartbeat (`data/bot_heartbeat`) обновляется каждые 30с для
  Docker healthcheck.

## MessageCoordinate (`core/queue.py`)

Per-chat воркер: один `asyncio.Task` на чат. `handle()` вызывается на каждое
входящее и только **сохраняет + будит** воркера; вся логика — в воркере.

### handle(): уведомление → сохранение

```
normalize_event → ensure_user/chat
→ download photo/voice (best-effort)
→ persist_media_file() в data/media
→ add_chunk(kind, media_path) + log_media()
→ push_message(chat_id, placeholder, kind)  [role="user"]
→ спит? queue_incoming_message() и выход
→ _pending[chat_id] += placeholder → _chat_user[chat_id] = user_id
→ encode current-batch media (image_url / input_audio)
→ event.set() → ensure worker task
```

Каноническое хранилище (`chunks`) и контекстный буфер пишутся немедленно, до
генерации — сообщения не теряются. Медиа также сохраняется в `MEDIA_DIR`, чтобы
его можно было повторно подключить к истории через несколько ходов.

Во время сна сообщение **сначала** пишется в `chunks` + `context_buffer`, затем
кладётся в `sleep_message_queue`; ответа нет. Это позволяет ночным суммаризаторам
увидеть его, а после пробуждения — обработать.

### Воркер: wake-on-every → typing-wait → generate

```
loop:
  1. _debounce(): wake-on-every — возврат на следующем тике, без окна тишины
     (MESSAGE_DEBOUNCE_SECONDS игнорируется; решение принимает сама Мила)
  2. typing-wait (если ENABLE_TYPING_WAIT):
     печатает → ждать сообщения / конца печати + grace / max wait;
     пришло сообщение → continue
  3. _generate_for_chat(): генерация; restart=True → continue
  4. _pending пуст → конец сессии батча
```

### Typing-поддержка (TDLib updateChatAction)

Состояние: `_typing_last[chat_id] = monotonic()` на каждый typing-like экшен;
`ChatActionCancel` снимает. `is_user_typing()` — свежесть
`TYPING_FRESHNESS_SECONDS` (6.0с).

- `_wait_typing()`: fast path (не печатает → 0 задержки) → цикл по
  `TYPING_POLL_INTERVAL` (0.5с) до `TYPING_WAIT_MAX_SECONDS` (8.0с);
  сообщение пришло → выход; печать кончилась → grace
  `TYPING_GRACE_SECONDS` (2.0с); max → «proceeding anyway».
- `_should_abort_for_typing()` = `ENABLE_TYPING_WAIT && TYPING_ABORT_GENERATION &&
  is_user_typing()`.
- `_interrupted()` = `message | typing | None`. Проверяется перед drain батча,
  на каждой итерации стрима, **перед каждой отправкой** и после генерации.
- При прерывании недоставленный батч возвращается в начало `_pending`
  (`_restore_pending`) — ход не теряется.

### _generate_for_chat()

1. Hard gate: если Мила спит → батч сбрасывается (сообщения уже сохранены).
2. Pre-check interruption (typing/message → restart).
3. Drain `_pending` → `batch_text` (`"\n".join`); пусто → выход.
4. Забрать `_batch_media` (фото/аудио текущего батча + event_lines); для
   текстового хода всё равно передаётся `{"chat_id": chat_id}` — пайплайн
   подтянет медиа из истории.
5. `bot._compress_context(chat_id)`.
6. **Needs pre-filter**: `fatigue > 0.85` и ночь (22–7ч) → auto-silence без LLM
   (`add_chunk(content="")` + `apply_action_effect("silence")`).
7. Без typing-индикатора во время решения — осознанно: уведомление не приказ.
8. `_build_histories` → снимок `TurnContext(turn_id, chat_id, user_id,
   presence)` → `process_message_stream(..., turn=ctx)`:
   - `thought` — обновляет `full_response`;
   - `action` — лог действия (отправка не здесь);
   - `reply` — `_send_bubbles(texts, ctx, via)`:
     pre-send interruption-check → проверка сна → **единый шлюз**
     `core.delivery.deliver_messages` (`send_typing` → пауза 0.3–1.0с →
     `send_text`; после каждого пузыря `add_chunk` + `push_message(
     role="assistant")` в тот же чат; для `via="send_message_brush_off"` —
     ещё и строка в глобальный `action_journal`);
   - `silence` — флаг, отправки нет;
   - `search_result` — лог;
   - `error` — лог.

   Получатель берётся из `ctx.resolve_target()`: владелец хода, а при
   `send_message` с алиасом / `enter_dialog` в proactive-ходе — явно
   разрешённая цель. Событие `reply` не несёт `chat_id`; прежняя формула
   `presence.focus_chat_id or req_chat` удалена. Если валидной цели нет —
   доставка блокируется (fail closed).
9. Post: interruption → restore + restart. Если ничего не отправлено —
   сохраняется только `chunks` с пустым `content` (в контекстный буфер пустой
   ответ не попадает). **Fallback-отправки `thoughts` больше нет.**

### Политика отправки

- В `rest`/`working` полноценный ответ возможен только после `enter_dialog`;
  текст без тула игнорируется пайплайном.
- `send_message` в rest/working — одна короткая отписка, не диалог.
- В `dialog` ответ — обычный текст, пузыри разделяются пустой строкой;
  пауза и typing между пузырями.
- **Получатель определяется владельцем хода** (`TurnContext.chat_id`), а не
  `presence.focus_chat_id`. `presence` задаёт только режим/промпт/тулы.
  Единственные исключения — явный `send_message` с алиасом и `enter_dialog`
  в proactive-ходе (они добавляют цель в `allowed`).
- Новое сообщение или печать собеседника во время генерации/отправки отменяют
  ход; батч возвращается в очередь.

## Presence (`core/presence.py`)

Одна строка `presence_state id=1` в `mila.db`, переживает рестарты:

- `rest` — дефолт; видит все уведомления, решает сама;
- `dialog` — фокус на одном чате (`focus_chat_id` + `focus_user_name`);
- `working` — «свои дела»; в v1 видимость как у rest;
- `sleep` — намерение; реальный цикл принадлежит sleep manager.

Переключаются тулами (`enter_dialog/enter_rest/enter_working/start_sleep`).
`format_state_block()` добавляет в system prompt «где ты» и кто пишет (имена,
не id); поведение лежит в `prompts/states/{rest,dialog,working}.md`.

**Auto-rest**: watcher (`start_auto_rest_scheduler`, тик
`DIALOG_IDLE_CHECK_INTERVAL_SECONDS`) вызывает `check_dialog_idle()`. Если Мила в
`dialog` и в focus-чате нет сообщений дольше
`DIALOG_IDLE_TIMEOUT_SECONDS` (300с), она возвращается в `rest` — но только если
собеседник не печатает в этот момент.

Sleep manager синхронизирует presence: старт сна → `sleep`, пробуждение/commit/
rollback → `rest`.

## tg_adapter (`core/tg_adapter.py`) + events/media

Вся pytdbot-специфика в одном месте, остальной код работает с plain-типами:

- `NormalizedMessage{chat_id, user_id, text, message_id, sender_name, chat_name}`
  — legacy для текста.
- `Notification` (`core/events.py`): `{kind, chat_id, user_id, text/caption,
  message_id, sender_name, chat_name, file_id, local_path, extra_hint}` +
  `placeholder_text()` / `llm_event_line()`.
- `classify_message_content()` — case-insensitive разбор
  (`messageText/messagePhoto/messageVoiceNote/...`), работает и с объектами
  pytdbot, и с dict-`@type`; фото выбирает biggest size, войс достаёт `file_id`,
  стикер — emoji + set name.
- `normalize_event()` — строит Notification для любого приватного сообщения,
  никогда не дропает и не бросает.
- `download_notification_media()` — `downloadFile(synchronous=True)` для
  photo/voice, лимит `MAX_MEDIA_MB`; путь пишется в `chunks.media_path` +
  `media_logs`.
- `is_text_message()` — только `MessageText` (совместимость, в hot path не
  используется).
- `is_private_chat()` — только `ChatTypePrivate`; группы игнорируются.
- `resolve_sender_name/resolve_chat_name`, `normalize_message`, `send_text`,
  `send_typing`, `parse_chat_action_update`, `is_typing_like_action`,
  `is_cancel_action`.

### Медиа (`core/media.py`)

- `encode_image_for_llm()` — ресайз до 1568px, JPEG quality 85, `data_url`.
- `encode_audio_for_llm()` — ogg/opus/m4a/flac → wav 16kHz mono через ffmpeg;
  поддержаны wav/mp3; файл >10 МБ пропускается (плейсхолдер остаётся).
- `persist_media_file()` — копия в `MEDIA_DIR` (TDLib-кэш может быть очищен).
- `build_multimodal_parts()` — текстовый content или список
  `{text, image_url, input_audio}`.
- В истории нативно видны последние 3 фото и 2 аудио; более старые остаются
  текстовыми плейсхолдерами.

## tg_auth (`core/tg_auth.py`) и create_session

- `register_auth_handlers(client, phone_hint, use_qr=False)`:
  `WaitPhoneNumber` (hint из `PHONE_NUMBER` или ввод) → `WaitCode` →
  `WaitPassword` (2FA) → `Ready`. Дополнительно обрабатываются
  `WaitEmailAddress` / `WaitEmailCode` / `WaitRegistration` /
  `WaitPremiumPurchase` / `WaitOtherDeviceConfirmation`; неизвестные состояния
  печатаются как `[auth] Состояние ...`.
- Ввод читается с fd 0 в daemon-потоке (не блокирует выход по таймауту).
  В Docker без TTY — предупреждение `_warn_no_tty()` (используйте
  `docker attach mila-bot`).
- Неверный пароль 2FA можно ввести повторно (до 5 попыток), неверный код — до
  3 попыток; пустой ввод повторяет запрос (попытку не расходует), Ctrl+C —
  выход; после исчерпания попыток скрипт просит перезапуск. Пароль 2FA
  вводится скрыто (`getpass`).
- `create_session.py` — one-shot: клиент TDLib → ожидание
  `AuthorizationStateReady` 300с → `getMe()` → печать пути `TD_FILES_DIRECTORY`.
  Поддерживает `--qr`. Обёртки: `start/create_session.sh` (временный venv,
  python3.11/uv, отказ от root) и `start/create_session.bat`.

### Код не приходит

- Способ доставки и длина кода печатаются из `code_info`.
  `authenticationCodeTypeTelegramMessage` — код приходит сообщением в сервисный
  чат **«Telegram» (777000)** в уже авторизованном приложении, а не по SMS.
  Если `next_type` пуст, резервной доставки (SMS/звонок) Telegram не предлагает.
- Таймаут 300с **не отправляет новый код**: TDLib восстанавливает `WaitCode` из
  `data/tdlib`. Повторный запуск покажет предупреждение «новый код НЕ
  отправлялся» — нужно ввести уже полученный код либо удалить `data/tdlib` и
  запросить код заново.
- Слишком частые запросы кода Telegram временно блокирует; после нескольких
  неудачных попыток нужно подождать несколько часов.
- `resend` работает только если `next_type` не пуст. Иначе Telegram отвечает
  `Authentication code can't be resend`.
- Альтернатива без кода — QR-вход:
  `rm -rf data/tdlib && ./start/create_session.sh --qr` (или
  `python create_session.py --qr`), затем отсканировать QR (рисуется в терминале
  через `qrcode`, рядом печатается ссылка `tg://login?token=...`) либо открыть
  ссылку на телефоне, где аккаунт уже авторизован. Ссылка обновляется
  автоматически — сканируйте последний QR.

## Настройки (`config.py`)

| Параметр | По умолч. | Описание |
|----------|-----------|----------|
| `MESSAGE_DEBOUNCE_SECONDS` | `2.0` | Legacy: игнорируется (wake-on-every) |
| `ENABLE_TYPING_WAIT` | `true` | Мастер-выключатель typing-логики |
| `TYPING_WAIT_MAX_SECONDS` | `8.0` | Верхняя граница ожидания печати |
| `TYPING_FRESHNESS_SECONDS` | `6.0` | Сколько typing-событие считается свежим |
| `TYPING_POLL_INTERVAL` | `0.5` | Шаг опроса |
| `TYPING_GRACE_SECONDS` | `2.0` | Ожидание сообщения после конца печати |
| `TYPING_ABORT_GENERATION` | `true` | Отменять генерацию/отправку при печати |
| `DIALOG_IDLE_TIMEOUT_SECONDS` | `300.0` | Тишина в диалоге → rest |
| `DIALOG_IDLE_CHECK_INTERVAL_SECONDS` | `30.0` | Период проверки auto-rest |
| `MAX_MEDIA_MB` | `20` | Лимит скачивания медиа |
| `MEDIA_DIR` | `data/media` | Персистентное хранение для истории |

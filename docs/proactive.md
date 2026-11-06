# Проактивность (Proactive Scheduler)

**Файл:** `core/proactive.py`

Планировщик позволяет Миле написать первой после длительной тишины — не в ответ
на уведомление, а по собственному «желанию». Решение принимает State Agent
(`core/needs/tracker.py`), исполнение — обычный пайплайн с доступом к
`peek_recent_chats`.

## Условия запуска

Фоновый цикл стартует в `MilaUserbot.start()` (`start_proactive_scheduler`) и
проверяет состояние:

- `proactive.enabled = true`;
- Мила не спит и не коммитит сон;
- presence — `rest` или `working` (не `dialog`);
- **глобальная тишина** (последний chunk в любом чате) ≥ `proactive.idle_minutes`
  (по умолчанию 27 минут);
- с прошлого запуска прошло ≥ `proactive.cooldown_minutes` (по умолчанию 27).

Периодичность проверки — `proactive.check_interval_seconds` (60с, клиппится
`10..3600`). Команды из Observatory опрашиваются каждые 10с, чтобы ручной
trigger срабатывал без ожидания интервала.

## Решение State Agent

```python
tracker.get_needs(force=True)      # глобальные потребности
tracker.run_inference(needs)       # MLP или rule-based
```

| Действие | Что происходит |
|----------|----------------|
| `write_to_user` | Один проактивный ход LLM: список чатов + опционально `peek_recent_chats` |
| `sleep` | `start_sleep(duration_minutes=proactive.sleep_minutes, triggered_by="state_agent")` |
| `wait` | Заглушка: событие в панель, ничего не отправляется |
| `plan_day` | Заглушка: событие в панель, ничего не отправляется |

Для `write_to_user` чаты выбираются `get_chats_by_last_activity(limit=max_chats)`,
от старых к свежим. В контекст добавляется блок с инструкциями:

1. можно сразу вызвать `enter_dialog` с `chat_alias` и ответить в диалоге;
2. можно сначала вызвать `peek_recent_chats`, чтобы посмотреть сообщения;
3. можно промолчать пустым ответом;
4. при желании — `start_sleep` на 480 минут;
5. писать только в один чат за ход.

`peek_recent_chats` доступен только в проактивном ходе
(`include_proactive=True`), в обычных ответах его нет.
Каждый чат показывается с именем и постоянным алиасом; он же передаётся в
`enter_dialog`, `send_message` или фильтр `chat_aliases` у `peek_recent_chats`.
Внутри приложения и при отправке по-прежнему используется настоящий `chat_id`.

## Защиты перед отправкой

Перед фактической отправкой проверяется:

- не пришло ли новое сообщение во время генерации (сравнение
  `get_global_last_activity()` до/после) — тогда отправка отменяется,
  реактивный воркер отвечает сам;
- не печатает ли собеседник в целевом чате;
- не начался ли сон;
- есть ли валидный `chat_id` (не `"proactive"`).

Если Мила вошла в диалог, но так и не отправила сообщение, presence
возвращается в `rest`, чтобы не остаться в чужом диалоге.

## Ручной запуск из Observatory

Страница State Agent → «Trigger & Simulate» или API:

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/state-agent/trigger` | Поставить одноразовую команду |
| GET | `/api/state-agent/trigger` | Статус команды и `last_run` |

Тело:

```json
{
  "dry_run": false,
  "force_action": "write_to_user",
  "needs_override": {
    "fatigue": 0.2,
    "need_social": 0.9,
    "boredom": 0.8,
    "seconds_since_last_msg": 7200,
    "current_hour": 14,
    "in_active_dialog": false
  }
}
```

- `dry_run = true` — только описывает, что произошло бы; LLM не вызывается,
  сообщения не отправляются, сон не стартует;
- `force_action` — пропустить инференс и выполнить указанное действие;
  допустимы `wait`, `sleep`, `write_to_user`, `plan_day`;
- `needs_override` применяется к живым потребностям перед инференсом;
- `force_action = null` — модель решает сама.

Команда кладётся в `runtime_config["proactive.command"]` с `run_now=true`.
Bot-процесс забирает её, сбрасывает флаг и пишет результат в
`proactive.last_run`:

```json
{
  "at": "2026-10-29T12:00:00+00:00",
  "action": "write_to_user",
  "probabilities": {"wait": 0.1, "sleep": 0.05, "write_to_user": 0.8, "plan_day": 0.05},
  "forced": false,
  "dry_run": false,
  "idle_seconds": 1800,
  "result": "sent 2 bubble(s) → 123456"
}
```

Ручной trigger обходит idle/cooldown, но **не** обходит защиты сна и presence.

## Конфигурация

Секция `proactive` в `runtime_config.json`:

| Ключ | По умолчанию | Описание |
|------|--------------|----------|
| `enabled` | `true` | Мастер-выключатель |
| `idle_minutes` | `27` | Глобальная тишина до запуска |
| `check_interval_seconds` | `60` | Период плановой проверки |
| `sleep_minutes` | `480` | Длительность сна по решению State Agent |
| `cooldown_minutes` | `27` | Минимум между запусками |
| `max_chats` | `20` | Сколько чатов попадает в контекст |
| `default_peek_limit` | `10` | Сообщений на чат в `peek_recent_chats` |
| `command` | — | Одноразовая команда из Observatory |
| `last_run` | `null` | Результат последнего запуска |

## Связанные ограничения

- Проактивный ход не использует `user_message`; в лог пайплайна попадает
  `proactive_block`.
- Отправка идёт через `tg_adapter.send_text` и пишется в `chunks` +
  `context_buffer` как assistant-сообщение.
- События `proactive`/`sleep` добавляются в in-memory ленту Dashboard.
- `wait` и `plan_day` пока не реализованы как действия — это осознанные
  заглушки, чтобы State Agent можно было развивать без изменения поведения.

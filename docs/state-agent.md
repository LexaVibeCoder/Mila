# State Agent (экспериментальный)

**Основные файлы:** `core/needs/tracker.py`, `core/proactive.py`,
`Mila_observatory/api/needs.py`, `state_agent/train_state_agent.py`,
`state_agent/test_interactive.py`, `state_agent/infer.py`

Экспериментальный модуль отслеживания «потребностей» Милы на основе PyTorch MLP
с rule-based fallback. Влияет на поведение через needs-блок в system prompt,
ночной авто-silence и проактивный планировщик.

> **Статус:** экспериментальный, но `NeedsTracker` уже в hot path
> (persona + queue + pipeline + proactive).

## Архитектура

```
state_agent/               # обучение + CLI
├── train_state_agent.py   # MLP + обучение
├── test_interactive.py    # интерактивный тест: 6 значений → action + вероятности
├── train_and_test.sh      # обучить (если нужно) + протестировать
├── train_and_test.bat     # то же для Windows
├── infer.py               # CLI инференс
├── dataset.json           # тренировочные сэмплы
├── state_agent.pt         # веса {state_dict, mean, std, config}
└── requirements.txt

core/needs/
└── tracker.py             # NeedsTracker + rule-based fallback

core/proactive.py          # планировщик, использующий инференс
Mila_observatory/api/
└── needs.py               # /api/state-agent*
```

## MLP модель

```python
MLP(
  Linear(6 → 128) → BatchNorm1d → ReLU → Dropout(0.2)
  → Linear(128 → 64) → BatchNorm1d → ReLU → Dropout(0.2)
  → Linear(64 → 4)
)
```

Архитектура задаётся `Config.hidden_dims = [128, 64]`. Веса загружаются из
`state_agent.pt` вместе с `mean/std/config`; инференс — z-score нормализация.

### Входные признаки (6)

| # | Признак | Описание |
|---|---------|----------|
| 0 | `seconds_since_last_msg` | Секунд с последнего чанка (`chunks.time_start`), иначе 86400 |
| 1 | `current_hour` | Текущий час (0–23) |
| 2 | `fatigue` | Усталость (0–1) |
| 3 | `need_social` | Потребность в общении (0–1) |
| 4 | `boredom` | Скука (0–1) |
| 5 | `in_active_dialog` | 1, если последнее сообщение < 300с назад |

### Выходные действия (4)

| Индекс | Действие | Описание |
|--------|----------|----------|
| 0 | `wait` | Ждать |
| 1 | `sleep` | Лечь спать |
| 2 | `write_to_user` | Написать пользователю |
| 3 | `plan_day` | Спланировать день (заглушка) |

## NeedsTracker

**Файл:** `core/needs/tracker.py`

### Расчёт потребностей

- `_compute_fatigue(hour, seconds, in_dialog)` — минимум утром, растёт к ночи
  (до ~0.95), +0.05 в активном диалоге, +0.1 при молчании > 10ч;
- `_compute_need_social(hour, seconds)` — днём 0.3→~1.0 по насыщению
  `1-1/(1+s/1800)` (+0.1 за >1ч, +0.1 за >2ч); ночью 0.1→0.4;
- `_compute_boredom(hour, seconds)` — днём 0.2→~1.0 (`s/1200`, +0.1 за >30м,
  +0.1 за >1ч); ночью 0.1→0.4;
- всё клиппится в `[0.05, 1.0]`, округляется до 4 знаков.

### Кэш и per-chat активность

`get_needs(force=False, chat_id=None)`:

- кэш на **2 секунды** (`_cache_ttl = 2.0`);
- ключ кэша включает `chat_id`, поэтому активность в чате A не сбрасывает
  `seconds_since_last_msg`/`in_active_dialog` для чата B;
- офсеты действий **глобальные** (у Милы одно «тело»);
- `force=True` — пересчёт.

`get_needs_summary(force=False)` вызывает `get_needs()` без `chat_id`
(глобальная активность) и форматирует блок для промпта
(`high >0.7 / medium >0.4 / low`).

### Офсеты действий

`apply_action_effect(action_type)` сдвигает потребности и сбрасывает кэш:

| Действие | Эффект |
|----------|--------|
| `send_message` | social −0.15 (мин −0.5) |
| `enter_dialog` | social −0.1 |
| `silence` / `enter_rest` | fatigue −0.05 |
| `start_sleep` | fatigue −0.1 |
| `search_memory` / `peek_recent_chats` | boredom −0.1 |
| `write_diary` | boredom −0.08, fatigue +0.03 |
| `update_diary` | boredom −0.04 |

Вызывается из пайплайна после каждого действия; `reset_offsets()` — при старте
сна. Значения клиппятся: social/boredom до −0.5, fatigue до −0.3/+0.3.

### Инференс

1. `get_needs()` через эвристики + офсеты.
2. `load_model()` — загрузка `state_agent.pt`; ошибка сохраняется в `_load_error`.
3. MLP-инференс с замером `inference_time_ms`.
4. Если модели нет → `_rule_based_inference()`:
   - `fatigue > 0.8` и ночь (hour ≥ 22 или ≤ 5) → `sleep`;
   - молчание > 30000с и утро (6–10ч) → `plan_day`;
   - активный диалог + (social > 0.5 или boredom > 0.5) → `write_to_user`;
   - social > 0.7 вне диалога → `write_to_user`;
   - иначе → `wait`.

### Прямое влияние на поведение

- **Needs-блок** в system prompt (`context/needs.md`).
- **Авто-silence** в `queue.py`: `fatigue > 0.85` ночью (22–7ч) отменяет
  генерацию до LLM.
- **Проактивность** (`core/proactive.py`): после 27 минут глобальной тишины
  `write_to_user` запускает ход LLM, `sleep` усыпляет Милу, `wait`/`plan_day` —
  заглушки (см. [proactive.md](proactive.md)).

### Синглтон

```python
from core.needs.tracker import get_tracker
tracker = get_tracker()
needs = tracker.get_needs(chat_id=chat_id)
summary = tracker.get_needs_summary()
result = tracker.run_inference(needs)
info = tracker.get_model_info()
```

## Dataset

**Файл:** `state_agent/dataset.json` — сэмплы:

```json
{
  "seconds_since_last_msg": 7200,
  "current_hour": 14,
  "fatigue": 0.3,
  "need_social": 0.7,
  "boredom": 0.6,
  "in_active_dialog": 1,
  "action": "write_to_user"
}
```

`dataset.json` и `state_agent.pt` хранятся в репозитории намеренно: модель
доступна сразу после клонирования, а обучение воспроизводимо. После локального
переобучения `state_agent.pt` изменится в рабочем дереве — коммить новый стейт
только вместе с соответствующим изменением кода/данных.

## API (монтируется на Observatory)

| Эндпоинт | Описание |
|----------|----------|
| `GET /api/state-agent` | Статус модели + текущие потребности + инференс |
| `POST /api/state-agent/run` | Принудительный инференс |
| `POST /api/state-agent/needs` | Override потребностей + инференс |
| `POST /api/state-agent/trigger` | Проактивный запуск (dry-run / force_action) |
| `GET /api/state-agent/trigger` | Статус команды и `last_run` |

## Обучение

### Самостоятельно

```bash
cd state_agent
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python train_state_agent.py

# CLI-инференс
.venv/bin/python infer.py --seconds 3600 --hour 14 --fatigue 0.3 \
  --social 0.7 --boredom 0.6 --dialog 1
```

### Автоматическое обучение/тестирование

```bash
./state_agent/train_and_test.sh              # обучение только если state_agent.pt отсутствует
./state_agent/train_and_test.sh --retrain    # принудительное переобучение
./state_agent/train_and_test.sh --no-test    # только обучение/проверка, без теста

state_agent\train_and_test.bat               # то же в Windows (те же флаги)
```

Скрипт использует `state_agent/.venv`: создаёт его при отсутствии (Python 3.11
или uv) и ставит `state_agent/requirements.txt` только если нужные импорты не
проходят. Существующий рабочий venv не пересоздаётся. Модель проверяется не
только на наличие файла, но и на загрузку с текущей архитектурой: если
`state_agent.pt` отсутствует или повреждён/несовместим, запускается обучение;
если загружается — обучение пропускается и скрипт сразу предлагает тест
(`--retrain` — форсировать).

После обучения `test_interactive.py` спрашивает шесть признаков; Enter
оставляет значение по умолчанию, `q` — выход. После каждого набора выводится
действие, вероятности всех четырёх классов и время инференса; запрос
повторяется, пока не введён `q`.

Гиперпараметры (`Config`):

- `epochs=200`, `batch_size=32`, `lr=1e-3`;
- `hidden_dims=[128, 64]`, `dropout=0.2`, `weight_decay=1e-4`, `patience=15`;
- `test_size=0.15`, `val_size=0.15` (stratified split), нормализация по train;
- `AdamW + ReduceLROnPlateau(factor=0.5, patience=5)` + `clip_grad_norm(1.0)`;
- сохраняется лучший стейт по `val_loss` → `state_agent.pt`;
- в конце — accuracy / classification report / confusion matrix на тесте.

Модель загружается по пути
`Mila/state_agent/state_agent.pt` (`MODEL_PATH` в `tracker.py`).

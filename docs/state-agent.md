# State Agent (экспериментальный)

**Основные файлы**: `core/needs/tracker.py`, `Mila_observatory/api/needs.py`, `state_agent/train_state_agent.py`, `state_agent/infer.py`

Экспериментальный модуль для отслеживания "потребностей" Милы на основе PyTorch MLP-модели. Влияет на поведение напрямую (needs-блок в system prompt, авто-silence при усталости) и позволяет Миле проявлять инициативу.

> **Статус**: экспериментальный, но `NeedsTracker` уже встроен в hot path (persona + pipeline + queue).

## Архитектура

```
state_agent/               # обучение + CLI
├── train_state_agent.py   # MLP + обучение
├── infer.py               # CLI инференс (--seconds/--hour/--fatigue/--social/--boredom/--dialog)
├── dataset.json           # тренировочные сэмплы (в репо)
├── state_agent.pt         # обученные веса {state_dict, mean, std, config}
├── requirements.txt
├── Dockerfile
└── docker-compose.yml     # сервисы train / infer

core/needs/                # рантайм
└── tracker.py             # NeedsTracker — трекер + rule-based fallback

Mila_observatory/api/      # API роутер
└── needs.py               # GET /api/state-agent, POST /api/state-agent/run|/needs
```

## MLP модель

```python
MLP(
  Linear(6 → 128) → BatchNorm1d → ReLU → Dropout(0.2)
  → Linear(128 → 64) → BatchNorm1d → ReLU → Dropout(0.2)
  → Linear(64 → 4)  # 4 действия
)
```

Архитектура задаётся `Config.hidden_dims = [128, 64]` (не 64→32). Веса загружаются из `state_agent.pt` вместе с `mean/std/config`; инференс — `predict(model, mean, std, features)` с z-score нормализацией.

### Входные признаки (6)

| # | Признак | Описание |
|---|---------|----------|
| 0 | `seconds_since_last_msg` | Секунд с последнего чанка в БД (`chunks.time_start`, иначе 86400) |
| 1 | `current_hour` | Текущий час (0-23) |
| 2 | `fatigue` | Уровень усталости (0-1) |
| 3 | `need_social` | Потребность в общении (0-1) |
| 4 | `boredom` | Скука (0-1) |
| 5 | `in_active_dialog` | 1, если последнее сообщение < 300с назад |

### Выходные действия (4)

| Индекс | Действие | Описание |
|--------|----------|----------|
| 0 | `wait` | Ждать |
| 1 | `sleep` | Лечь спать |
| 2 | `write_to_user` | Написать пользователю |
| 3 | `plan_day` | Спланировать день |

## NeedsTracker

**Файл**: `core/needs/tracker.py`

### Компоненты потребностей

- `_compute_fatigue(hour, seconds, in_dialog)` — минимум утром (0.1), растёт к ночи (до ~0.95), +0.05 в активном диалоге, +0.1 при молчании > 10ч
- `_compute_need_social(hour, seconds)` — днём 0.3→~1.0 по насыщению `1-1/(1+s/1800)` (+0.1 за >1ч, +0.1 за >2ч); ночью 0.1→0.4
- `_compute_boredom(hour, seconds)` — днём 0.2→~1.0 (`s/1200`, +0.1 за >30м, +0.1 за >1ч); ночью 0.1→0.4
- всё клиппится в [0.05, 1.0], округляется до 4 знаков
- `get_needs()` кэшируется на **2 секунды** (`_cache_ttl = 2.0`); `force=True` — пересчёт

### Офсеты действий (память поведения)

`apply_action_effect(action_type)` сдвигает потребности и сбрасывает кэш:
- `send_message`: social −0.15 (мин −0.5)
- `silence`: fatigue −0.05 (мин −0.3)
- `search_memory`: boredom −0.1 (мин −0.5)
- `write_diary`: boredom −0.08, fatigue +0.03 (макс +0.3)
- `update_diary`: boredom −0.04

Вызывается из пайплайна после **каждого** исполненного action; `reset_offsets()` — при старте сна. `get_needs_summary()` форматирует блок для промпта (`high >0.7 / medium >0.4 / low`) с подсказками при fatigue/social/boredom > 0.7.

### Инференс

1. `get_needs()` через эвристики (+ офсеты)
2. `load_model()` — загрузка `state_agent.pt` (ошибка запоминается в `_load_error`)
3. MLP-инференс с замером `inference_time_ms`
4. Если модели нет → `_rule_based_inference()`:
   - `fatigue > 0.8 + ночь (hour ≥ 22 или ≤ 5)` → sleep
   - молчание > 30000с + утро (6–10ч) → plan_day
   - активный диалог + (social > 0.5 или boredom > 0.5) → write_to_user
   - social > 0.7 вне диалога → write_to_user
   - иначе → wait

Прямое влияние на поведение сегодня: **авто-silence в `queue.py`** — `fatigue > 0.85` ночью (22–7ч) отменяет генерацию до LLM. Решение MLP (`run_inference`) пока потребляется через API/Observatory, а не автоматическим планировщиком.

### Синглтон

```python
from core.needs.tracker import get_tracker
tracker = get_tracker()
needs = tracker.get_needs()            # кэш 2с
summary = tracker.get_needs_summary()  # блок для system prompt
result = tracker.run_inference(needs)  # {"action", "probabilities", "inference_time_ms", "model_loaded", "fallback"}
info = tracker.get_model_info()        # {"loaded", "error", "model_path", ...}
```

## Dataset

**Файл**: `state_agent/dataset.json` — сэмплы в формате:

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

## API (монтируется на Observatory)

**Файл**: `Mila_observatory/api/needs.py`

| Эндпоинт | Описание |
|----------|----------|
| `GET /api/state-agent` | Статус трекера + текущие потребности |
| `POST /api/state-agent/run` | Запустить инференс |
| `POST /api/state-agent/needs` | Переопределить потребности вручную + инференс |

## Обучение

```bash
cd state_agent
pip install -r requirements.txt
python train_state_agent.py
# или Docker:
docker-compose up train
```

Актуальные гиперпараметры (`train_state_agent.py`, dataclass `Config`):
- `epochs=200`, `batch_size=32`, `lr=1e-3`
- `hidden_dims=[128, 64]`, `dropout=0.2`, `weight_decay=1e-4`, `patience=15` (early stopping по `val_loss`)
- `test_size=0.15`, `val_size=0.15` (stratified split), нормализация по train, `AdamW + ReduceLROnPlateau(factor=0.5, patience=5)` + `clip_grad_norm(1.0)`
- сохраняется лучший стейт по `val_loss` → `state_agent.pt{state_dict, mean, std, config}`; в конце — accuracy/classification report/confusion matrix на тесте

## Docker (отдельный)

```yaml
services:
  train:  # обучение модели
    build: .
    command: python train_state_agent.py
  infer:  # CLI инференс
    build: .
    command: python infer.py --seconds 3600 --hour 14 --fatigue 0.3 --social 0.7 --boredom 0.6 --dialog 1
```

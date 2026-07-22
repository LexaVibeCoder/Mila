# State Agent (экспериментальный)

**Основные файлы**: `core/needs/tracker.py`, `Mila_observatory/api/needs.py`

Экспериментальный модуль для отслеживания "потребностей" Милы на основе PyTorch MLP-модели. Позволяет Миле проявлять инициативу: написать пользователю первой, лечь спать, спланировать день.

> **Статус**: экспериментальный.

## Архитектура

```
state_agent/               # обучение + CLI
├── train_state_agent.py   # MLP + обучение
├── infer.py               # CLI инференс
├── dataset.json           # 52 тренировочных сэмпла
├── state_agent.pt         # Обученная модель
├── requirements.txt
├── Dockerfile
└── docker-compose.yml

core/needs/                # рантайм
└── tracker.py             # NeedsTracker — трекер + rule-based fallback

Mila_observatory/api/      # API роутер
└── needs.py               # FastAPI роутер (монтируется на Observatory)
```

## MLP модель

```python
MLP(
  Linear(6 → 64) → BatchNorm1d → ReLU → Dropout(0.3)
  → Linear(64 → 32) → BatchNorm1d → ReLU → Dropout(0.3)
  → Linear(32 → 4)  # 4 действия
)
```

### Входные признаки (6)

| # | Признак | Описание |
|---|---------|----------|
| 0 | `seconds_since_last_msg` | Секунд с последнего сообщения в чате |
| 1 | `current_hour` | Текущий час (0-23) |
| 2 | `fatigue` | Уровень усталости (0-1) |
| 3 | `need_social` | Потребность в общении (0-1) |
| 4 | `boredom` | Скука (0-1) |
| 5 | `in_active_dialog` | Флаг активного диалога (0/1) |

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

- `_compute_fatigue()` — на основе текущего часа: пик в 3-5 ночи, минимум днём
- `_compute_need_social()` — растёт с каждым часом без сообщений, пик в 15-21
- `_compute_boredom()` — растёт с каждым часом без сообщений, пик днём

### Инференс

1. Вычисление потребностей через эвристики
2. Загрузка `state_agent.pt` (если доступна)
3. MLP-инференс с z-score нормализацией (mean/std из датасета)
4. Если модели нет → `_rule_based_inference()`:
   - `fatigue > 0.8 + ночь` → sleep
   - долгое молчание + утро → plan_day
   - активный диалог + social/boredom → write_to_user
   - иначе → wait

### Сиглтон

```python
from core.needs.tracker import get_tracker
tracker = get_tracker()
needs = tracker.compute_needs()
result = tracker.run_inference(needs)
```

## Dataset

**Файл**: `dataset.json` — 52 сэмпла в формате:

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

Гиперпараметры (`train_state_agent.py`, dataclass `Config`):
- `epochs=200`, `batch_size=16`, `lr=0.001`
- `weight_decay=1e-5`, `patience=30` (early stopping)
- Соотношение train/val: 80/20
- Сохраняется лучшая модель по `val_f1`

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

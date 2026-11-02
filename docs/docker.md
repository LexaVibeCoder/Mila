# Docker развёртывание

Mila поставляется с multi-stage `Dockerfile` и `docker-compose.yml`
(сервисы `bot` + `observatory`). Поддерживаемый способ запуска — Docker
Compose; отдельная установка Python/Node на хосте не нужна.

## Быстрый старт

```bash
cp .env.example .env
# заполните API_ID, API_HASH, LLM_*, EMBED_*
./start/docker.sh
```

`start/docker.sh` проверит `.env`, наличие образа, TDLib-сессию и reranker.
Если контейнеры уже запущены, скрипт их не трогает; если остановлены —
запускает через `docker compose start`; если их нет — создаёт через
`docker compose up -d --no-recreate`. Без скрипта:

```bash
docker compose up -d --build
```

Первый вход в Telegram — только внутри контейнера (см. ниже). После запуска
панель доступна по адресу из `.env`, по умолчанию
`http://127.0.0.1:8080`.

## Схема работы `start/docker.sh`

```text
./start/docker.sh
  ├─ docker + compose доступны?
  ├─ .env есть? ──нет──> создать из .env.example и выйти
  ├─ обязательные значения .env? ──нет──> показать ключи и выйти
  ├─ образ есть? ──нет──> docker compose build
  ├─ TDLib-сессия есть? ──нет──> предложить войти (--login)
  ├─ reranker есть? ──нет──> предложить скачать
  ├─ контейнеры:
  │    ├─ running  ──> не трогать
  │    ├─ stopped  ──> docker compose start
  │    └─ missing  ──> docker compose up -d --no-recreate
  └─ вывести URL + docker compose ps
```

Осознанное обновление:

- `--recreate` — пересоздать контейнеры из текущего образа
  (`up -d --force-recreate`);
- `--rebuild` — пересобрать образ без кэша и затем пересоздать контейнеры;
- `--restart` — просто перезапустить существующие контейнеры;
- изменения в `docker-compose.yml` по умолчанию не применяются, пока не
  запустить `--recreate`.

## Переменные Observatory

Порт и интерфейсы не зашиты жёстко: значения берутся из `.env`, в compose
остались только безопасные значения по умолчанию.

| Переменная | По умолчанию | Где используется |
|------------|--------------|------------------|
| `OBSERVATORY_PORT` | `8080` | Порт внутри контейнера и опубликованный порт на хосте |
| `OBSERVATORY_PUBLISH_HOST` | `127.0.0.1` | Интерфейс хоста для `ports:` |
| `OBSERVATORY_BIND_HOST` | `0.0.0.0` | Адрес прослушивания внутри контейнера |

Примеры:

```ini
# Только локально (по умолчанию)
OBSERVATORY_PORT=8080
OBSERVATORY_PUBLISH_HOST=127.0.0.1

# Доступ по LAN — обязательно задайте пароль
OBSERVATORY_PUBLISH_HOST=0.0.0.0
OBSERVATORY_PASSWORD=change-me

# Другой порт
OBSERVATORY_PORT=9090
```

`OBSERVATORY_BIND_HOST` менять обычно не нужно: внутри контейнера панель должна
слушать `0.0.0.0`, иначе опубликованный порт до неё не достучится.

Для обратной совместимости, если `OBSERVATORY_PUBLISH_HOST` не задан, compose
использует `OBSERVATORY_HOST` (старое имя переменной). Новым конфигурациям
следует задавать `OBSERVATORY_PUBLISH_HOST`.

## docker-compose.yml

```yaml
services:
  bot:
    build: { context: ., dockerfile: Dockerfile }
    container_name: mila-bot
    user: root
    restart: unless-stopped
    stdin_open: true          # TDLib-auth через `docker attach mila-bot`
    tty: true                 # detach: Ctrl-P Ctrl-Q
    env_file: [.env]
    volumes:
      - .:/app
      - mila_data:/app/data
      - mila_logs:/app/logs
      - ./models:/app/models  # rw: install_reranker.py загружает модель сюда
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    command: python main.py
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import os,time,sys; p='data/bot_heartbeat'; sys.exit(0 if (os.path.exists(p) and time.time()-os.path.getmtime(p)<120) else 1)\""]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks: [mila_network]

  observatory:
    build: { context: ., dockerfile: Dockerfile }
    container_name: mila-observatory
    user: root
    restart: unless-stopped
    env_file: [.env]
    environment:
      OBSERVATORY_HOST: ${OBSERVATORY_BIND_HOST:-0.0.0.0}
    ports:
      - "${OBSERVATORY_PUBLISH_HOST:-${OBSERVATORY_HOST:-127.0.0.1}}:${OBSERVATORY_PORT:-8080}:${OBSERVATORY_PORT:-8080}"
    volumes:
      - .:/app:z
      - mila_data:/app/data
      - mila_logs:/app/logs
      - ./models:/app/models
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    command: python -m Mila_observatory.server
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:$${OBSERVATORY_PORT:-8080}/api/health || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
    depends_on: [bot]
    networks: [mila_network]

volumes: { mila_data:, mila_logs: }
networks: { mila_network: { driver: bridge } }
```

Замечания:

- `runtime_config.json` лежит в корне и шарится через bind-mount `.:/app` —
  правки из Observatory видит бот без рестарта (mtime-синхронизация).
- `bot` не имеет HTTP-порта; healthcheck смотрит на `data/bot_heartbeat`
  (обновляется каждые 30с, считается свежим < 120с).
- `observatory` зависит от `bot`; healthcheck — публичный `/api/health`.
- `$${OBSERVATORY_PORT}` в healthcheck экранирован, чтобы Compose не подставлял
  значение на хосте: контейнер читает свой `OBSERVATORY_PORT` из `.env`.
- Внутри контейнера панель по умолчанию слушает `0.0.0.0` (его собственный
  `127.0.0.1` недоступен извне). Наружу порт публикуется на
  `${OBSERVATORY_PUBLISH_HOST:-${OBSERVATORY_HOST:-127.0.0.1}}:${OBSERVATORY_PORT:-8080}`.

## Dockerfile (multi-stage)

```dockerfile
# Stage 1: frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app
COPY Mila_observatory/frontend/package.json Mila_observatory/frontend/package-lock.json ./
RUN npm ci
COPY Mila_observatory/frontend/ ./
RUN npm run build              # → /app/dist

# Stage 2: Python app
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git libc6-dev ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend-builder /app/dist Mila_observatory/frontend/dist
RUN mkdir -p "data/photos" "data/daily reports" "data/media" "data/tdlib" logs models
CMD ["python", "main.py"]
```

`ffmpeg` нужен для конвертации голосовых в wav; `curl` — для healthcheck
Observatory. `EXPOSE` намеренно не указан: порт задаётся через
`OBSERVATORY_PORT` в compose и не должен быть зашит в образ.

## Volumes

| Volume | Контейнерный путь | Назначение |
|--------|-------------------|------------|
| `mila_data` | `/app/data` | `mila.db`, `context_buffer.db`, FAISS, `daily reports/`, `media/`, `tdlib/` |
| `mila_logs` | `/app/logs` | `pipeline_*.json`, `diary_logs/` |
| `./models` (bind) | `/app/models` | Модели, в т.ч. BGE-reranker; **rw**, чтобы работал `install_reranker.py` |
| bind `.:/app` | `/app` | Код + `runtime_config.json` + `frontend/dist` |
| `/etc/localtime`, `/etc/timezone` | `:ro` | Часовой пояс при `LOCAL_TIMEZONE=""` |

> `mila_data` — named volume. TDLib-сессия живёт внутри него, поэтому логин
> нужно выполнять в контейнере, а не на хосте. Удаление volume удалит сессию,
> базу и память.

## Первый вход в Telegram

```bash
# по коду из Telegram
./start/docker.sh --login

# по QR-ссылке (без кода)
./start/docker.sh --login --qr
```

Вручную:

```bash
docker compose stop bot
docker compose run --rm -it bot python create_session.py
# или: docker compose run --rm -it bot python create_session.py --qr
docker compose up -d
```

Важно: не запускайте `create_session.py` на хосте и не держите два клиента на
одном `data/tdlib` — TDLib-сессия не рассчитана на параллельный доступ.
`docker attach mila-bot` остаётся альтернативой, если бот уже запущен с TTY
(detach — `Ctrl-P Ctrl-Q`).

## Установка reranker модели

```bash
./start/docker.sh --reranker
```

Вручную:

```bash
docker compose run --rm bot python install_reranker.py
```

Скачивает `BAAI/bge-reranker-v2-m3` в путь из `RERANKER_MODEL_PATH` (по
умолчанию `models/rerankers/BGE-Reranker-v2-m3/`) на хосте; каталог `./models`
смонтирован read-write. Пропускает, если каталог уже существует и не пуст.
Без reranker-а `search_memory` возвращает ошибку.

## Пересборка фронтенда

Код и `runtime_config.json` видны контейнерам через bind-mount без пересборки
образа, но Python-изменения применяются после `docker compose restart`. Фронт
(Vite SPA) нужно собирать в `Mila_observatory/frontend/dist` на хосте.
Локальный Node не обязателен:

```bash
docker run --rm -v "$(pwd)/Mila_observatory/frontend:/app" -w /app \
  node:20-alpine sh -c "npm ci && npm run build"
docker compose restart observatory
```

Windows PowerShell: `$(pwd)` → `${PWD}`. После обновления страницы используйте
Ctrl+F5 — имя бандла в `dist/assets` меняется при каждой сборке.

Если `dist` отсутствовал в момент старта Observatory, SPA-роут не
зарегистрируется — после сборки обязателен `restart observatory`.

## Команды

```bash
./start/docker.sh              # проверки + запуск/переиспользование контейнеров
./start/docker.sh --login      # первый вход по коду
./start/docker.sh --login --qr # первый вход по QR
./start/docker.sh --reranker   # скачать reranker
./start/docker.sh --logs       # логи в реальном времени
./start/docker.sh --restart    # перезапустить существующие сервисы
./start/docker.sh --status     # статус контейнеров
./start/docker.sh --down       # остановить (volumes сохраняются)
./start/docker.sh --recreate   # пересоздать из текущего образа
./start/docker.sh --rebuild    # пересобрать без кэша + пересоздать
./start/docker.sh --no-build   # не собирать образ
```

Эквиваленты через Compose:

```bash
docker compose up -d --build
docker compose up -d --no-recreate
docker compose up -d --force-recreate
docker compose start
docker compose up -d bot
docker compose up -d observatory
docker compose build --no-cache
docker compose logs -f bot
docker compose logs -f observatory
docker compose stop bot
docker compose run --rm -it bot python create_session.py
```

## Требования

- Docker Engine 20.10+;
- Docker Compose 2.x (`docker compose`, не `docker-compose`);
- Linux/macOS для `start/docker.sh`; на Windows используйте `docker compose`
  напрямую (или WSL).

## Частые проблемы

| Симптом | Причина / решение |
|---------|-------------------|
| `database is locked` | Два процесса пишут в SQLite; WAL/busy_timeout уже настроены, не держите долгие транзакции |
| Панель не открывается с хоста | Проверьте `OBSERVATORY_PUBLISH_HOST`/`OBSERVATORY_PORT` в `.env`; `docker compose port observatory 8080` (или ваш `OBSERVATORY_PORT`) покажет фактический mapping |
| Порт занят | Задайте другой `OBSERVATORY_PORT` в `.env` и перезапустите |
| 401 в панели | Задан `OBSERVATORY_PASSWORD`; введите `OBSERVATORY_USERNAME`/пароль |
| Панель открыта всей сети | `OBSERVATORY_PUBLISH_HOST=0.0.0.0` без пароля; задайте `OBSERVATORY_PASSWORD` или верните `127.0.0.1` |
| Голосовые не слышны | Нет `ffmpeg` в образе или файл > `MAX_MEDIA_MB` |
| TDLib-сессия «сломалась» | Сменился `TD_DATABASE_ENCRYPTION_KEY`; верните прежний ключ или удалите `mila_data` и войдите заново |
| Reranker не скачивается | Проверьте сеть и права на `./models`; каталог смонтирован rw |
| Фронт старый | Пересоберите `dist`, `docker compose restart observatory`, обновите страницу с Ctrl+F5 |
| Изменения `docker-compose.yml` не применились | По умолчанию существующие контейнеры не пересоздаются; запустите `./start/docker.sh --recreate` |

# Docker развёртывание

Mila поставляется с multi-stage `Dockerfile` и `docker-compose.yml`
(сервисы `bot` + `observatory`).

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
      - ./models:/app/models:ro
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
    ports:
      - "127.0.0.1:8080:8080"   # localhost-only по умолчанию
    volumes:
      - .:/app:z
      - mila_data:/app/data
      - mila_logs:/app/logs
      - ./models:/app/models:ro
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    command: python -m Mila_observatory.server
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8080/api/health || exit 1"]
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
- Панель слушает `0.0.0.0` внутри контейнера, но наружу проброшена только на
  `127.0.0.1`. Для доступа по LAN замените на `"8080:8080"` **и** задайте
  `OBSERVATORY_PASSWORD` (Basic Auth).
- TDLib-авторизация в контейнере: `docker attach mila-bot` (телефон/код/2FA,
  detach — `Ctrl-P Ctrl-Q`), либо один раз на хосте через
  `create_session.py` / `start/create_session.*`.

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
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git libc6-dev ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend-builder /app/dist Mila_observatory/frontend/dist
RUN mkdir -p "data/photos" "data/daily reports" "data/media" "data/tdlib" logs models
EXPOSE 8080
CMD ["python", "main.py"]
```

`ffmpeg` нужен для конвертации голосовых в wav; `curl` — для healthcheck
Observatory.

## Volumes

| Volume | Контейнерный путь | Назначение |
|--------|-------------------|------------|
| `mila_data` | `/app/data` | `mila.db`, `context_buffer.db`, FAISS, `daily reports/`, `media/`, `tdlib/` |
| `mila_logs` | `/app/logs` | `pipeline_*.json`, `diary_logs/` |
| `./models` (bind) | `/app/models:ro` | Модели (BGE-reranker и др.) read-only |
| bind `.:/app` | `/app` | Код + `runtime_config.json` + собранный `frontend/dist` |
| `/etc/localtime`, `/etc/timezone` | `:ro` | Часовой пояс при `LOCAL_TIMEZONE=""` |

> Named volume `frontend_dist` больше не используется: он замораживал фронт на
> первой сборке. Фронт всегда берётся из bind-mount/образа.

## Пересборка фронтенда

Бэкенд подхватывается живьём (bind), фронт (Vite SPA) нужно собирать. Локальный
node не обязателен:

```bash
docker run --rm -v "$(pwd)/Mila_observatory/frontend:/app" -w /app \
  node:20-alpine sh -c "npm ci && npm run build"
docker compose up -d --build observatory
```

Windows PowerShell: `$(pwd)` → `${PWD}`. После обновления страницы используйте
Ctrl+F5 — имя бандла в `dist/assets` меняется при каждой сборке.

## Запуск

```bash
# Полный запуск
docker compose up -d --build

# Только бот / только Observatory
docker compose up -d bot
docker compose up -d observatory

# Сборка без кэша
docker compose build --no-cache

# Логи
docker compose logs -f bot
docker compose logs -f observatory

# TDLib-авторизация внутри контейнера
docker attach mila-bot   # detach: Ctrl-P Ctrl-Q
```

## Требования

- Docker Engine 20.10+;
- Docker Compose 2.x (`docker compose`, не `docker-compose`);
- Python 3.11+ (без Docker);
- Node.js 20+ (для сборки фронтенда без Docker).

## Запуск без Docker

```bash
# 0. Установка с нуля (идемпотентно: venv, зависимости, .env, сессия, reranker)
./start/install.sh                          # Windows: start\install.bat

# 1. TDLib-сессия (один раз; install.sh уже делает это)
.venv/bin/python create_session.py          # или ./start/create_session.sh --qr

# 2. Бот
.venv/bin/python main.py

# 3. Панель (опционально)
.venv/bin/python -m Mila_observatory.server
# Windows: Mila_Observatory.bat
```

## Установка reranker модели

```bash
.venv/bin/python install_reranker.py
```

Скачивает `BAAI/bge-reranker-v2-m3` в
`models/rerankers/BGE-Reranker-v2-m3/` (совпадает с `RERANKER_MODEL_PATH`).
Пропускает, если каталог уже существует. Без reranker-а `search_memory`
возвращает ошибку.

## Частые проблемы

| Симптом | Причина / решение |
|---------|-------------------|
| `database is locked` | Два процесса пишут в SQLite; WAL/busy_timeout уже настроены, не держите долгие транзакции |
| Панель не открывается с хоста | Проверьте `127.0.0.1:8080:8080` и что сервер слушает `0.0.0.0` |
| 401 в панели | Задан `OBSERVATORY_PASSWORD`; введите `OBSERVATORY_USERNAME`/пароль |
| Голосовые не слышны | Нет `ffmpeg` в окружении или файл > 10 МБ |
| TDLib-сессия «сломалась» | Сменился `TD_DATABASE_ENCRYPTION_KEY`; верните прежний ключ или создайте fresh `data/tdlib` |
| Фронт старый | Пересоберите `dist` и обновите страницу с Ctrl+F5 |

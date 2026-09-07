# Docker развёртывание

Mila поставляется с multi-stage Dockerfile и docker-compose.yml (сервисы `bot` + `observatory`).

## docker-compose.yml

```yaml
services:
  bot:
    build: { context: ., dockerfile: Dockerfile }
    container_name: mila-bot
    user: root
    restart: unless-stopped
    stdin_open: true   # TDLib-auth через `docker attach mila-bot`
    tty: true          # (detach: Ctrl-P Ctrl-Q)
    env_file: [.env]
    volumes:
      - .:/app
      - mila_data:/app/data
      - mila_logs:/app/logs
      - ./models:/app/models:ro
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    command: python main.py
    networks: [mila_network]

  observatory:
    build: { context: ., dockerfile: Dockerfile }
    container_name: mila-observatory
    user: root
    restart: unless-stopped
    env_file: [.env]
    ports: ["8080:8080"]
    volumes:
      - .:/app:z
      - frontend_dist:/app/Mila_observatory/frontend/dist
      - mila_data:/app/data
      - mila_logs:/app/logs
      - ./models:/app/models:ro
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    command: uvicorn Mila_observatory.server:app --host 0.0.0.0 --port 8080 --reload
    depends_on: [bot]
    networks: [mila_network]

volumes: { mila_data:, mila_logs:, frontend_dist: }
networks: { mila_network: { driver: bridge } }
```

Замечания:
- `runtime_config.json` лежит в корне и шарится через bind-mount `.:/app` (а не named volume) — правки из Observatory видит бот без рестарта (mtime-синхронизация).
- `bot` зависит ни от чего; `observatory` — `depends_on: [bot]`.
- TDLib-авторизация в контейнере: `docker attach mila-bot` (ввод телефона/кода/2FA), либо один раз на хосте через `python create_session.py` / `start/create_session.*`.

## Dockerfile (multi-stage)

```dockerfile
# Stage 1: Frontend build
FROM node:20-alpine AS frontend-builder
WORKDIR /app
COPY Mila_observatory/frontend/package.json Mila_observatory/frontend/package-lock.json ./
RUN npm ci
COPY Mila_observatory/frontend/ ./
RUN npm run build   # → /app/dist

# Stage 2: Python app
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend-builder /app/dist Mila_observatory/frontend/dist
RUN mkdir -p data/photos data/daily_reports logs models
EXPOSE 8080
CMD ["python", "main.py"]
```

## Volumes

| Volume | Контейнерный путь | Назначение |
|--------|-------------------|------------|
| `mila_data` | `/app/data` | `mila.db`, `context_buffer.db`, FAISS `faiss_text.index`, `daily reports/`, `photos/`, `tdlib/` |
| `mila_logs` | `/app/logs` | Логи пайплайна `pipeline_*.json`, `diary_logs/` |
| `./models` (bind) | `/app/models:ro` | AI модели (BGE-reranker и др.) read-only |
| `frontend_dist` | `/app/Mila_observatory/frontend/dist` | Собранный фронтенд (перекрывает COPY из образа) |
| bind `.:/app` | `/app` | Код + `runtime_config.json` (мост bot ↔ observatory) |
| `/etc/localtime`, `/etc/timezone` | `:ro` | Часовой пояс (для `LOCAL_TIMEZONE=""`) |

## Запуск

```bash
# Полный запуск
docker compose up -d

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

- Docker Engine 20.10+
- Docker Compose 2.x+ (`docker compose`, не `docker-compose`)
- Python 3.11+ (без Docker)
- Node.js 20+ (для сборки фронтенда без Docker)

## Запуск без Docker

```bash
# 1. Создать сессию TDLib (один раз; спросит телефон/код/2FA)
python create_session.py
# Windows/Linux-обёртки: start/create_session.bat / start/create_session.sh

# 2. Запустить бота
python main.py

# 3. (опционально) Запустить веб-панель
python -m Mila_observatory.server
# Windows: Mila_Observatory.bat (venv + сервер + браузер на :8080)
```

## Установка reranker модели

```bash
python install_reranker.py
```

Скачивает `BAAI/bge-reranker-v2-m3` в `models/rerankers/BGE-Reranker-v2-m3/` (совпадает с `RERANKER_MODEL_PATH`). Пропускает, если каталог уже существует.

# Docker развёртывание

Mila поставляется с multi-stage Dockerfile и docker-compose.yml.

## docker-compose.yml

```yaml
services:
  bot:
    build: .
    container_name: mila_bot
    command: python main.py
    volumes:
      - mila_data:/app/data
      - mila_logs:/app/logs
      - ./models:/app/models:ro
      - ./mila_session.session:/app/mila_session.session
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    restart: unless-stopped
    depends_on:
      - observatory   # необязательно, но удобно

  observatory:
    build: .
    container_name: mila_observatory
    command: python -m Mila_observatory.server
    ports:
      - "8080:8080"
    volumes:
      - mila_data:/app/data
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    restart: unless-stopped
```

## Dockerfile (multi-stage)

```dockerfile
# Stage 1: Frontend build
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
COPY Mila_observatory/frontend/package*.json ./
RUN npm ci
COPY Mila_observatory/frontend/ ./
RUN npm run build

# Stage 2: Python app
FROM python:3.11-slim
WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Built frontend from stage 1
COPY --from=frontend /app/frontend/dist Mila_observatory/frontend/dist

CMD ["python", "main.py"]
```

## Volumes

| Volume | Контейнерный путь | Назначение |
|--------|-------------------|------------|
| `mila_data` | `/app/data` | SQLite БД, FAISS индекс, daily reports, runtime config |
| `mila_logs` | `/app/logs` | Логи пайплайна, diary logs |
| `./models` (bind) | `/app/models:ro` | AI модели (BGE-reranker, CLIP, etc.) read-only |
| `./mila_session.session` (bind) | `/app/mila_session.session` | Pyrogram сессия |

## Запуск

```bash
# Полный запуск
docker-compose up -d

# Только бот
docker-compose up -d bot

# Только Observatory
docker-compose up -d observatory

# Сборка без кэша
docker-compose build --no-cache

# Логи
docker-compose logs -f bot
```

## Требования

- Docker Engine 20.10+
- Docker Compose 2.x+
- Python 3.11+ (без Docker)
- Node.js 20+ (для сборки фронтенда без Docker)

## Запуск без Docker

```bash
# 1. Создать сессию (один раз)
python create_session.py

# 2. Запустить бота
python main.py

# 3. (опционально) Запустить веб-панель
python -m Mila_observatory.server
```

## Установка reranker модели

```bash
python install_reranker.py
```

Скачивает BGE-reranker-v2-m3 в `models/bge-reranker-v2-m3/`.

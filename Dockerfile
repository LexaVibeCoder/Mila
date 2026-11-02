# ============================================================
# Stage 1: Build Observatory Frontend (React/Vite)
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /app
COPY Mila_observatory/frontend/package.json Mila_observatory/frontend/package-lock.json ./
RUN npm ci

COPY Mila_observatory/frontend/ ./
RUN npm run build

# ============================================================
# Stage 2: Main Python image
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# Unbuffered output makes `docker compose logs` show lines immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install system dependencies for torch, sentence-transformers, faiss
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    git \
    libc6-dev \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Copy built frontend from stage 1
COPY --from=frontend-builder /app/dist Mila_observatory/frontend/dist

# Create data directories (will be mounted as volumes in production)
# NOTE: DAILY_REPORTS path contains a space by design (config.py DAILY_REPORTS).
RUN mkdir -p "data/photos" "data/daily reports" "data/media" "data/tdlib" logs models

# The Observatory port is published by docker-compose from OBSERVATORY_PORT.
# EXPOSE is intentionally omitted: it is informational only and would
# hardcode a single port into the image.

# Default command: run the Telegram bot. Observatory is a separate service.
CMD ["python", "main.py"]

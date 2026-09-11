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

# Expose observatory port
EXPOSE 8080

# Default command: run both bot and observatory via a supervisor script
CMD ["python", "main.py"]
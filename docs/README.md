# Документация Mila

Mila — Telegram-юзербот с LLM-рассуждением, векторной памятью FAISS, дневником и веб-панелью Observatory (FastAPI + React).

## Содержание

| Раздел | Описание |
|--------|----------|
| [Архитектура](architecture-overview.md) | Общая архитектура проекта, потоки данных, диаграммы связей |
| [Основной пайплайн](pipeline.md) | `ThinkPipeline` — цикл рассуждения с тулзами, потоковая обработка |
| [Система памяти](memory.md) | FAISS-векторная память + SQLite `memories` + автобиографическая память Милы |
| [Контекстный буфер](context-buffer.md) | Per-chat SQLite-буфер контекста, сжатие по токенам, ротация |
| [Тулзы](tools.md) | Система инструментов: `think()`, `search_memory`, `send_message`, `silence`, `write_diary` |
| [LLM клиент](llm-client.md) | OpenAI-совместимый обёртка, стриминг, реконфигурация на ходу |
| [Персона](persona.md) | Системный промпт, биография Милы, сборка контекста |
| [Ночная рефлексия](sleep-system.md) | Ночной цикл: анализ дневника, суммаризация диалогов, извлечение фактов |
| [State Agent](state-agent.md) | Экспериментальный PyTorch-трекер потребностей (усталость, социалка, скука) |
| [Observatory](observatory.md) | FastAPI + React веб-панель, API-эндпоинты, управление |
| [Конфигурация](config.md) | Два слоя конфигов: `.env` (статичный) и `runtime_config.json` (динамический) |
| [Базы данных](database.md) | Схема SQLite `mila.db` и `context_buffer.db` |
| [Docker](docker.md) | Docker-развёртывание, multi-stage сборка, volumes |

## Быстрый старт

```bash
python create_session.py   # один раз: создать сессию Pyrogram
python main.py             # запуск бота
python -m Mila_observatory.server  # запуск веб-панели (отдельно)
```

## Команды разработки

```bash
python -m Mila_observatory.server        # Observatory standalone
cd Mila_observatory/frontend && npm run dev  # Vite dev (порт 5173)
cd Mila_observatory/frontend && npm run build  # TypeScript + production build
```

# Документация Mila

Mila — Telegram-юзербот (TDLib/pytdbot) с LLM-рассуждением (`think`), векторной памятью FAISS, дневником, ночной рефлексией и веб-панелью Observatory (FastAPI + React).

## Содержание

| Раздел | Описание |
|--------|----------|
| [Архитектура](architecture-overview.md) | Общая архитектура проекта, потоки данных, паттерны |
| [Сообщения](messaging.md) | Уведомления, presence-состояния, мультимодальный вход, typing-aware ожидание/отмена, TDLib-адаптер и auth |
| [Основной пайплайн](pipeline.md) | `ThinkPipeline` — цикл рассуждения с тулзами, потоковая обработка |
| [Система памяти](memory.md) | FAISS-векторная память + SQLite `memories` + автобиографическая память |
| [Контекстный буфер](context-buffer.md) | Per-chat SQLite-буфер контекста, сжатие по токенам, ротация |
| [Дневник](diary.md) | `diary_entries`, категории, daily-файлы, интеграция с промптом |
| [Тулзы](tools.md) | `think()`, 5 действий: send/search/silence/write_diary/update_diary |
| [LLM клиент](llm-client.md) | OpenAI-совместимая обёртка, стриминг, реконфигурация на ходу |
| [Персона](persona.md) | System prompt из `core/llm/prompts/*.md`, динамический контекст |
| [Ночная рефлексия](sleep-system.md) | Ночной цикл: анализ дневника, суммаризация диалогов, извлечение фактов |
| [State Agent](state-agent.md) | Трекер потребностей (MLP + rule-based), офсеты, авто-silence |
| [Observatory](observatory.md) | FastAPI + React веб-панель, API-эндпоинты, управление |
| [Конфигурация](config.md) | Два слоя: `.env` (статичный) и `./runtime_config.json` (динамический) |
| [Базы данных](database.md) | Схема `mila.db` и `context_buffer.db`, FAISS |
| [Docker](docker.md) | Docker-развёртывание, multi-stage сборка, volumes |

## Быстрый старт

```bash
cp .env.example .env   # заполнить API_ID/API_HASH, LLM_*, EMBED_*
python create_session.py   # один раз: TDLib-логин (телефон/код/2FA)
python install_reranker.py # скачать BGE-Reranker-v2-m3 в models/
python main.py             # запуск бота
python -m Mila_observatory.server  # веб-панель :8080 (отдельно)
```

Windows: `start/create_session.bat` (сессия), `Mila_Observatory.bat` (панель + браузер).
Docker: `docker compose up -d` (сервисы `bot` + `observatory`).

## Команды разработки

```bash
python -m Mila_observatory.server              # Observatory standalone
cd Mila_observatory/frontend && npm run dev    # Vite dev (порт 5173)
cd Mila_observatory/frontend && npm run build  # tsc + production build → dist/
cd state_agent && python train_state_agent.py  # обучение State Agent
python test_tg_mila_dialogue.py                # E2E-тест диалога через Telegram
```

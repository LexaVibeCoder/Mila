# LLM Клиент

**Файл**: `core/llm_client.py`

OpenAI-совместимая обёртка для работы с LLM. Поддерживает горячую реконфигурацию, стриминг, ретраи с экспоненциальной задержкой.

## Класс LLMClient

```python
class LLMClient:
    def __init__(self, model, api_base, api_key, temperature=0.7, max_tokens=4096)
```

Создаёт `openai.AsyncOpenAI` внутри.

### Параметры

| Параметр | Описание |
|----------|----------|
| `model` | Название модели (например, `gpt-4`, `llama3`) |
| `api_base` | Базовый URL (должен заканчиваться на `/v1`) |
| `api_key` | API ключ |
| `temperature` | Температура генерации |
| `max_tokens` | Максимальное количество токенов в ответе |
| `tool_choice` | По умолчанию `"auto"` |

### Методы

#### `generate(messages, tools, tool_choice, system_prompt, max_retries)`

Не-стрим вызов:

```python
result = await client.generate(
    messages=[{"role": "user", "content": "Привет"}],
    tools=tool_definitions,
    tool_choice="required",
    system_prompt=system_prompt
)
```

**Возвращает**:
```python
{
    "role": "assistant",
    "content": "...",          # текстовый ответ (если есть)
    "tool_calls": [...],       # вызовы функций
    "finish_reason": "tool_calls" | "stop" | "length"
}
```

**Ретраи**: до 3 попыток с экспоненциальным backoff (5s, 25s, 125s).

#### `generate_stream(messages, tools, tool_choice, system_prompt)`

Стрим-вызов — возвращает AsyncGenerator, yield-ит токены.

```python
async for token in client.generate_stream(...):
    if token == "__tool_call__:<json>":
        # модель решила вызвать функцию
    else:
        # обычный токен текста
```

При ошибке стриминга — fallback на не-стрим версию.

### Реконфигурация

```python
client.reconfigure(
    model="new-model",
    api_base="http://new-url/v1",
    api_key="new-key",
    temperature=0.5,
    max_tokens=2048
)
```

Позволяет менять любой параметр на ходу. Создаёт новый `AsyncOpenAI` клиент внутри.

## LLMManager

**Файл**: `Mila_observatory/llm_manager.py`

Синглтон для управления LLM-клиентами через Observatory API.

```python
class LLMManager:
    def get_main_client() -> LLMClient       # с runtime overrides
    def get_main_params() -> dict
    def configure_main(params: dict)         # runtime config + reconfigure
    def test_query(prompt, client_type, params) -> dict  # тестовый запрос
```

### Runtime overrides

При каждом вызове `get_main_client()`:

1. Читает `runtime_config.get("llm")` и `runtime_config.get("night_llm")`
2. Создаёт или реконфигурит `LLMClient` с переопределёнными параметрами
3. Если runtime-параметры не заданы — использует статичные из `config.py`

## Интеграция с пайплайном

```
ThinkPipeline
    │
    ├── LLMClient.generate(..., tool_choice="required")
    │   └── модель ОБЯЗАНА вызвать think()
    │
    ├── парсинг tool_call → actions
    │
    ├── экзекуция тулзов
    │
    └── если search_memory → повтор с обогащённым контекстом
```

## Дневной суммаризатор

В `_ensure_summarizer()` создаётся отдельный `LLMClient` с более дешёвой моделью (можно настроить через runtime config) для сжатия контекста днём.

## Обработка ошибок

- 3 ретрая с экспоненциальной задержкой
- При стриме: fallback на не-стрим при ошибке
- Валидация вызова `think()` — если LLM не вызвала функцию, повтор с явным указанием
- Все ошибки логируются в `add_event()` Observatory

## Конфигурация

**Статичная** (config.py):
```python
LLM_API_BASE = "http://..."
LLM_API_KEY = "..."
MAIN_LLM_MODEL = "model-name"
MAIN_TEMPERATURE = 0.7
MAIN_MAX_TOKENS = 4096
```

**Динамическая** (runtime_config.json):
```json
{
  "llm": {
    "model": "override-model",
    "api_base": "http://override-url/v1",
    "temperature": 0.5
  }
}
```

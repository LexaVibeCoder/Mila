# LLM Клиент

**Файл**: `core/llm/client.py`

OpenAI-совместимая обёртка для работы с LLM. Поддерживает горячую реконфигурацию, стриминг, ретраи с экспоненциальной задержкой. Базовый URL нормализуется `normalize_api_base()` (достройка `/v1`).

## Класс LLMClient

```python
class LLMClient:
    def __init__(self, model=None, api_base=None, api_key=None,
                 max_tokens=None, temperature=None,
                 top_p=None, frequency_penalty=None, presence_penalty=None)
```

Создаёт `openai.AsyncOpenAI` внутри. `None`-параметры берутся из `config.py`: `LLM_API_BASE / LLM_API_KEY / MAIN_LLM_MODEL / MAIN_LLM_MAX_TOKENS (2048) / MAIN_LLM_TEMPERATURE (0.7)`; `top_p/frequency_penalty/presence_penalty` по умолчанию `None` (не отправляются, пока не заданы).

### Методы

#### `generate(messages, tools, tool_choice, system_prompt, max_retries, ...)`

Не-стрим вызов:

```python
result = await client.generate(
    messages=[{"role": "user", "content": "Привет"}],
    tools=tool_definitions,
    tool_choice="required",
    system_prompt=system_prompt
)
```

`system_prompt` (если задан) просто препендится как `{"role": "system", ...}` к messages. `tools/tool_choice` отправляются только если передан `tools`.

**Возвращает**:
```python
{
    "role": "assistant",
    "thoughts": "...",           # message.content (поле называется thoughts, не content!)
    "tool_calls": [...],         # вызовы функций или None
    "finish_reason": "tool_calls" | "stop" | "length"
}
```

**Ретраи**: до 3 попыток, задержка `retry_delay * 2^(attempt-1)` (по умолчанию `retry_delay=2.0` → 2с, 4с). Ретраятся: исключения API, пустые `choices`, пустой контент без tool_calls. Отдельный фолбэк: если гейтвей отверг нативный аудио-вход (`input_audio`) — один ретрай текстом без аудио-партов (плейсхолдер в истории остаётся), затем обычные ретраи.

#### `generate_stream(messages, tools, tool_choice, system_prompt)`

Стрим-вызов — AsyncGenerator строк:

```python
async for token in client.generate_stream(...):
    if token.startswith("__tool_call__:"):
        tool_calls = json.loads(token[len("__tool_call__:"):])
    else:
        # обычный текстовый токен (content по мере генерации)
```

Дельты tool_call склеиваются по `index` (`id/name/arguments` конкатенируются), в конце yield'ится один маркер `__tool_call__:<json>`. При ошибке стрима — **fallback на не-стрим `generate()`** (с тем же system_prompt) и yield его результата в том же формате.

> Пайплайн (`ThinkPipeline`) использует только `generate()`; `generate_stream` — для будущих/внешних потребителей.

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

Меняет только переданные (`not None`) параметры. Смена `api_base` и/или `api_key` пересоздаёт внутренний `AsyncOpenAI` (base через `normalize_api_base`).

## LLMManager

**Файл**: `Mila_observatory/llm_manager.py`

Синглтон для управления LLM-клиентами через Observatory API.

```python
class LLMManager:
    def get_main_client() -> LLMClient       # + runtime overrides на каждый вызов
    def get_main_params() -> dict            # main + runtime merge (ключ маскирован)
    def get_night_params() -> dict           # только runtime night_llm (ключ маскирован)
    def configure_main(params: dict)         # runtime set + reconfigure (кроме tool_choice)
    def configure_night(params: dict)        # только runtime set (подхват без рестарта)
    async def test_query(prompt, client_type="main", params=None) -> dict
```

Допустимые поля обоих `configure_*`: `model/api_base/api_key/temperature/max_tokens/top_p/frequency_penalty/presence_penalty/tool_choice` (`tool_choice` хранится в runtime, но в `LLMClient` не пробрасывается).

### Runtime overrides

При каждом `get_main_client()`:

1. Читает `runtime_config.get("llm.{model,api_base,api_key,temperature,max_tokens,top_p,frequency_penalty,presence_penalty}")`
2. Не-`None` значения применяются через `client.reconfigure()`
3. `None` → остаётся значение из `config.py`

### test_query (playground)

Всегда идёт через **main-клиент** (`client_type` игнорируется для выбора клиента, возвращается в ответе), `max_retries=1`, дефолт `max_tokens=500`:

```python
{"success": True, "response": thoughts, "timing": "1.23s", "model": ..., "client_type": ...}
# или {"success": False, "error": ..., "timing": ..., "model": ..., "client_type": ...}
```

## Интеграция с пайплайном

```
ThinkPipeline
    │
    ├── tool_choice = runtime_config.get("llm.tool_choice", "required")
    ├── LLMClient.generate(..., tool_choice)
    │   └── модель должна вызвать think(thoughts, actions)
    │
    ├── парсинг tool_call → actions (1 ретрай на 1-й итерации)
    │
    ├── экзекуция тулзов + NeedsTracker.apply_action_effect()
    │
    └── если search_memory(searched) → повтор с обогащённым контекстом
```

## Дневной суммаризатор

`MilaUserbot._ensure_summarizer()` лениво создаёт обычный `LLMClient` на `MAIN_LLM_MODEL / LLM_API_BASE / LLM_API_KEY` (отдельной настраиваемой «дешёвой» модели **нет**). Используется для сжатия контекста: `temperature=0.3, max_tokens=300`, промпт «Summarize ... 2-3 sentences, only facts».

## Обработка ошибок

- 3 ретрая с экспоненциальной задержкой (2с → 4с)
- Пустые ответы (нет choices / нет контента и нет tool_calls) ретрятся так же
- При стриме: fallback на не-стрим при ошибке
- Валидация вызова `think()` — system-ошибка в messages + 1 ретрай на первой итерации
- Все ошибки логируются в консоль и в `logs/pipeline_*.json` (`success: false, error, traceback`)

## Конфигурация

**Статичная** (config.py):
```python
LLM_API_BASE = "http://..."
LLM_API_KEY = "..."
MAIN_LLM_MODEL = "model-name"
MAIN_LLM_TEMPERATURE = 0.7
MAIN_LLM_MAX_TOKENS = 2048
```

**Динамическая** (`./runtime_config.json`, сидируется из статичной):
```json
{
  "llm": {
    "model": "override-model",
    "api_base": "http://override-url/v1",
    "api_key": "...",
    "temperature": 0.5,
    "max_tokens": 2048,
    "tool_choice": "auto"
  },
  "night_llm": { "model": "...", "temperature": 0.3, "max_tokens": 4096, "tool_choice": "required" }
}
```

# LLM Клиент

**Файл:** `core/llm/client.py`

OpenAI-совместимая обёртка: горячая реконфигурация, ретраи с backoff,
fallback при отказе от нативного аудио. Базовый URL нормализуется
`normalize_api_base()` (достройка `/v1`).

## Класс LLMClient

```python
LLMClient(
    model=None,
    api_base=None,
    api_key=None,
    max_tokens=None,
    temperature=None,
    top_p=None,
    frequency_penalty=None,
    presence_penalty=None,
    timeout=90.0,
    reasoning_effort=None,   # "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
    extra_body=None,         # dict провайдерских полей
)
```

`None`-параметры берутся из `config.py`: `LLM_API_BASE`, `LLM_API_KEY`,
`MAIN_LLM_MODEL`, `MAIN_LLM_MAX_TOKENS` (2048), `MAIN_LLM_TEMPERATURE` (0.7).
`api_key` можно передать только из `.env`-значений (ночной LLM использует
`get_night_llm_api_key()`); runtime config ключи не хранит.
`top_p/frequency_penalty/presence_penalty` по умолчанию `None` — не отправляются,
пока не заданы. Внутри создаётся `openai.AsyncOpenAI`.

### `generate(...)`

```python
result = await client.generate(
    messages=[{"role": "user", "content": "Привет"}],
    tools=tool_definitions,          # optional
    tool_choice="auto",
    system_prompt=system_prompt,     # optional
    max_tokens=None,
    temperature=None,
    max_retries=3,
    retry_delay=2.0,
)
```

- `system_prompt` препендится как `{"role": "system", ...}`;
- `tools`/`tool_choice` отправляются только если передан `tools`;
- `reasoning_effort`/`extra_body` добавляются, если заданы (см. ниже);
- возвращает:

```python
{
    "role": "assistant",
    "thoughts": "...",           # message.content (поле называется thoughts, не content)
    "tool_calls": [...] | None,
    "finish_reason": "tool_calls" | "stop" | "length",
    "reasoning": "..." | "",      # message.reasoning / reasoning_content, best-effort
}
```

### Think effort / reasoning

`reasoning_effort` — строка (`none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`); пустое/`None` — параметр не отправляется.

- **OpenRouter** (`api_base` содержит `openrouter.ai`): SDK не принимает
  `reasoning_effort` верхнего уровня, поэтому значение уходит как
  `extra_body={"reasoning": {"effort": ...}}`; существующий `extra_body`
  мержится.
- **OpenAI / LM Studio / Ollama / llama.cpp**: плоский `reasoning_effort`.
  Для нестандартных полей (например
  `{"chat_template_kwargs": {"enable_thinking": true}}`) используйте
  `extra_body`.
- `reasoning_effort="none"` отправляется явно: это важно для моделей, которые
  размышляют по умолчанию (gpt-5.x), но может не поддерживаться некоторыми
  не-reasoning моделями.
- Ответное reasoning возвращается в ключе `reasoning`, но в историю
  пайплайна не добавляется.

### Ретраи

- до `max_retries` попыток (по умолчанию 3);
- задержка `retry_delay * 2^(attempt-1)` + jitter 0–0.5с (2с, 4с);
- если провайдер вернул `Retry-After` для 429/5xx/timeout — задержка берётся
  из заголовка (клиппится `1..60с`) + jitter;
- пустой `choices` ретраится;
- **пустой контент без tool_calls — валидный ответ** (молчание или tool-only
  ход) и не ретраится;
- при исчерпании попыток бросает последнюю ошибку.

### Fallback без аудио

Если запрос содержит `input_audio` и провайдер его отверг, клиент один раз
повторяет запрос без аудио-партов. Текстовая история и плейсхолдеры сохраняются;
вместо чистого аудио-парта ставится
`[голосовое сообщение (аудио не поддерживается моделью)]`.

### Стриминга нет

Метода `generate_stream()` в текущем коде **нет**. `ThinkPipeline` использует
только `generate()`. Старые упоминания стриминга токенов устарели.

### Реконфигурация

```python
client.reconfigure(
    model="new-model",
    api_base="http://new-url/v1",
    temperature=0.5,
    max_tokens=2048,
    timeout=120.0,
    reasoning_effort="medium",
    extra_body={"chat_template_kwargs": {"enable_thinking": True}},
)
```

Меняет только переданные (`not None`) параметры. Смена `api_base` пересоздаёт
`AsyncOpenAI` с ключом из `.env` (`settings.LLM_API_KEY`); API-ключ через
`reconfigure()` не меняется.

## LLMManager

**Файл:** `Mila_observatory/llm_manager.py`

Синглтон для управления клиентами через Observatory API.

```python
get_main_client() -> LLMClient       # применяет runtime overrides
get_main_params() -> dict            # без api_key
get_night_params() -> dict           # только runtime night_llm, без api_key
configure_main(params)               # runtime + reconfigure клиента
configure_night(params)              # только runtime (подхват без рестарта)
test_query(prompt, client_type="main", params=None) -> dict
```

Допустимые поля: `model/api_base/temperature/max_tokens/top_p/
frequency_penalty/presence_penalty/tool_choice/reasoning_effort/extra_body`.
`tool_choice` хранится в runtime, но в `LLMClient` не пробрасывается (его
читает пайплайн). `reasoning_effort/extra_body` хранятся в runtime и
пробрасываются в клиент.
`api_key` через API не передаётся и не меняется: ключи только из `.env`.

### Runtime overrides

При каждом `get_main_client()`:

1. читаются `llm.{model,api_base,temperature,max_tokens,top_p,
   frequency_penalty,presence_penalty,reasoning_effort,extra_body}`;
2. не-`None` значения применяются через `reconfigure()`;
3. `None` → остаётся значение из `config.py`.

`configure_main()` дополнительно вызывает `reconfigure()` с переданными полями,
кроме `tool_choice`.

### Маскировка ключей

- ответы API содержат `abcd1234...wxyz`, а не полный ключ;
- `configure_*` пропускает значения с `...` (`should_keep_existing`), чтобы
  замаскированный ключ из GET не затёр реальный;
- `get_main_params()`/`get_night_params()` маскируют ключ на выходе.

### test_query (playground)

Всегда идёт через **main-клиент** (`client_type` возвращается в ответе),
`max_retries=1`, дефолт `max_tokens=500`:

```python
{"success": True, "response": thoughts, "timing": "1.23s", "model": ..., "client_type": ...}
# или {"success": False, "error": ..., "timing": ..., "model": ..., "client_type": ...}
```

## Интеграция с пайплайном

```
ThinkPipeline
    ├── system_prompt собирается заново каждую итерацию
    ├── tool_choice = "auto" (runtime "required" переопределяется)
    ├── LLMClient.generate(...) без response_format
    ├── tool_calls → direct actions
    ├── ответ без tools в dialog → текст, пузыри через пустую строку
    └── действия → NeedsTracker.apply_action_effect()
```

## Дневной суммаризатор

`MilaUserbot._ensure_summarizer()` лениво создаёт обычный `LLMClient` на
`MAIN_LLM_MODEL / LLM_API_BASE / LLM_API_KEY`. Используется для сжатия
контекста: `temperature=0.3`, `max_tokens=300`, промпт «Summarize ... 2-3
sentences, only facts».

## Обработка ошибок

- 3 ретрая с экспоненциальной задержкой и поддержкой `Retry-After`;
- пустой `choices` ретраится; пустой контент без tools — валиден;
- при отказе от аудио — один текстовый ретрай;
- ошибки логируются в консоль и в `logs/pipeline_*.json`
  (`success: false, error, traceback`).

## Конфигурация

**Статичная** (`config.py`):

```python
LLM_API_BASE = "http://..."
LLM_API_KEY = "..."
MAIN_LLM_MODEL = "model-name"
MAIN_LLM_TEMPERATURE = 0.7
MAIN_LLM_MAX_TOKENS = 2048
```

**Динамическая** (`runtime_config.json`, сидируется из статичной):

```json
{
  "llm": {
    "model": "override-model",
    "api_base": "http://override-url/v1",
    "temperature": 0.5,
    "max_tokens": 2048,
    "tool_choice": "auto",
    "reasoning_effort": "none",
    "extra_body": {}
  },
  "night_llm": {
    "model": "...",
    "temperature": 0.3,
    "max_tokens": 4096,
    "tool_choice": "required",
    "reasoning_effort": "none",
    "extra_body": {}
  }
}
```

Ключи (`LLM_API_KEY`, опциональный `NIGHT_LLM_API_KEY`) живут только в `.env`.

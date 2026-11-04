import copy
import openai
from typing import List, Dict, Any, Optional
from config import settings, normalize_api_base


def _is_openrouter(api_base: Optional[str]) -> bool:
    return "openrouter.ai" in (api_base or "").lower()


def _reasoning_kwargs(api_base: Optional[str], reasoning_effort: Optional[str],
                      extra_body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build request kwargs for reasoning/extra-body options.

    OpenAI-compatible servers (OpenAI, LM Studio, Ollama, llama.cpp) take a
    flat `reasoning_effort`. OpenRouter expects the nested
    `{"reasoning": {"effort": ...}}` object, which the SDK only forwards via
    `extra_body`.
    """
    body = copy.deepcopy(extra_body) if isinstance(extra_body, dict) and extra_body else None
    effort = (reasoning_effort or "").strip().lower()
    out: Dict[str, Any] = {}

    if effort:
        if _is_openrouter(api_base):
            body = body or {}
            existing = body.get("reasoning")
            reasoning = dict(existing) if isinstance(existing, dict) else {}
            reasoning["effort"] = effort
            body["reasoning"] = reasoning
        else:
            out["reasoning_effort"] = effort

    if body:
        out["extra_body"] = body
    return out


def _retry_wait(exc: Exception, attempt: int, base_delay: float) -> float:
    """Backoff with Retry-After support for 429/5xx/timeouts."""
    import random
    default = base_delay * (2 ** (attempt - 1))
    try:
        status = getattr(exc, "status_code", None)
        msg = str(exc).lower()
        retryable = (
            (isinstance(status, int) and (status == 429 or 500 <= status <= 599))
            or "429" in msg or "rate limit" in msg or "retry" in msg
            or "timeout" in msg or "temporar" in msg or "5xx" in msg
            or "overloaded" in msg or "try again" in msg
        )
        if not retryable and attempt == 1:
            # Unknown errors still get one fast retry; backoff grows anyway.
            pass
        # Honor Retry-After header when the SDK exposes it.
        retry_after = None
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None) if resp is not None else None
        if headers:
            try:
                ra = headers.get("retry-after") or headers.get("Retry-After")
                if ra:
                    retry_after = float(str(ra).split(",")[0].strip())
            except (TypeError, ValueError):
                retry_after = None
        if retry_after is not None:
            return min(max(retry_after, 1.0), 60.0) + random.uniform(0, 0.5)
    except Exception:
        pass
    import random as _r
    return default + _r.uniform(0, 0.5)


class LLMClient:
    def __init__(self, model: str = None, api_base: str = None, api_key: str = None,
                 max_tokens: int = None, temperature: float = None,
                 top_p: float = None, frequency_penalty: float = None,
                 presence_penalty: float = None, timeout: float = 90.0,
                 reasoning_effort: Optional[str] = None,
                 extra_body: Optional[Dict[str, Any]] = None):
        base = api_base or settings.LLM_API_BASE
        self.api_base = base
        self.client = openai.AsyncOpenAI(
            base_url=normalize_api_base(base),
            api_key=api_key or settings.LLM_API_KEY,
            timeout=timeout,
        )
        self.model = model or settings.MAIN_LLM_MODEL
        self.default_max_tokens = max_tokens or settings.MAIN_LLM_MAX_TOKENS
        self.default_temperature = temperature or settings.MAIN_LLM_TEMPERATURE
        self.default_top_p = top_p
        self.default_frequency_penalty = frequency_penalty
        self.default_presence_penalty = presence_penalty
        self.default_reasoning_effort = reasoning_effort
        self.default_extra_body = extra_body
        self.timeout = timeout

    def reconfigure(self, model: str = None, api_base: str = None,
                    max_tokens: int = None, temperature: float = None,
                    top_p: float = None, frequency_penalty: float = None,
                    presence_penalty: float = None, timeout: float | None = None,
                    reasoning_effort: Optional[str] = None,
                    extra_body: Optional[Dict[str, Any]] = None):
        if timeout is not None:
            self.timeout = timeout
        if model is not None:
            self.model = model
        if api_base is not None:
            # The key always comes from .env; runtime config cannot override it.
            self.api_base = api_base
            self.client = openai.AsyncOpenAI(
                base_url=normalize_api_base(api_base),
                api_key=settings.LLM_API_KEY,
                timeout=self.timeout,
            )
        if reasoning_effort is not None:
            self.default_reasoning_effort = reasoning_effort
        if extra_body is not None:
            self.default_extra_body = extra_body
        if max_tokens is not None:
            self.default_max_tokens = max_tokens
        if temperature is not None:
            self.default_temperature = temperature
        if top_p is not None:
            self.default_top_p = top_p
        if frequency_penalty is not None:
            self.default_frequency_penalty = frequency_penalty
        if presence_penalty is not None:
            self.default_presence_penalty = presence_penalty
        print(f"[LLMClient] Reconfigured: model={self.model}, temp={self.default_temperature}")

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> dict:
        import time
        import asyncio

        if max_tokens is None:
            max_tokens = self.default_max_tokens
        if temperature is None:
            temperature = self.default_temperature

        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages

        kwargs = dict(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature
        )

        if self.default_top_p is not None:
            kwargs["top_p"] = self.default_top_p
        if self.default_frequency_penalty is not None:
            kwargs["frequency_penalty"] = self.default_frequency_penalty
        if self.default_presence_penalty is not None:
            kwargs["presence_penalty"] = self.default_presence_penalty

        kwargs.update(_reasoning_kwargs(
            self.api_base, self.default_reasoning_effort, self.default_extra_body
        ))

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        last_error = None
        audio_stripped = False
        for attempt in range(1, max_retries + 1):
            try:
                start_time = time.time()
                response = await self.client.chat.completions.create(**kwargs)
                elapsed = time.time() - start_time
                print(f"  [LLM] Response in {elapsed:.2f}s")
            except Exception as e:
                # Fallback: if the model/gateway rejects native audio, retry
                # text-only (placeholder stays in history) instead of failing.
                if not audio_stripped and _contains_audio_part(kwargs.get("messages")):
                    print(f"  [LLM] Audio rejected ({e}), retrying text-only...")
                    audio_stripped = True
                    kwargs["messages"] = _strip_audio_parts(kwargs.get("messages"))
                    await asyncio.sleep(1.0)
                    continue
                print(f"  [LLM] API Error (attempt {attempt}): {e}")
                last_error = e

                if attempt < max_retries:
                    wait = _retry_wait(e, attempt, retry_delay)
                    print(f"  [LLM] Retry in {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                else:
                    raise

            if not response.choices:
                print(f"  [LLM] Empty response choices (attempt {attempt}), retrying...")
                last_error = Exception("Empty response from LLM API")
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay * (2 ** (attempt - 1)))
                    continue
                else:
                    raise Exception("Empty response from LLM API after all retries")

            choice = response.choices[0]
            message = choice.message

            has_tool_calls = hasattr(message, 'tool_calls') and message.tool_calls
            # Empty content with no tools is valid: rest/working silence,
            # a dialog "no answer", or a tool-only turn. Only missing
            # choices are retried above.

            tool_calls = None
            if has_tool_calls:
                tool_calls = message.tool_calls

            return {
                "role": message.role,
                "thoughts": message.content or "",
                "tool_calls": tool_calls,
                "finish_reason": choice.finish_reason,
                # OpenRouter returns `reasoning`, local servers `reasoning_content`.
                "reasoning": _extract_reasoning(message),
            }

        raise last_error or Exception("Empty response from LLM API")


def _extract_reasoning(message) -> Any:
    """Best-effort reasoning text; SDK versions expose it differently."""
    for attr in ("reasoning", "reasoning_content"):
        value = getattr(message, attr, None)
        if value:
            return value
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning", "reasoning_content"):
            value = extra.get(key)
            if value:
                return value
    return ""


def _contains_audio_part(messages) -> bool:
    try:
        for m in messages or []:
            c = (m or {}).get("content")
            if isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and p.get("type") == "input_audio":
                        return True
    except Exception:
        pass
    return False


def _strip_audio_parts(messages):
    out = []
    for m in messages or []:
        c = (m or {}).get("content")
        if isinstance(c, list):
            kept = [p for p in c
                    if not (isinstance(p, dict) and p.get("type") == "input_audio")]
            if not kept:
                kept = [{"type": "text", "text": "[голосовое сообщение (аудио не поддерживается моделью)]"}]
            elif all(isinstance(p, dict) and p.get("type") != "text" for p in kept):
                kept = [{"type": "text", "text": "[вложение]"}] + kept
            m = {**m, "content": kept}
        out.append(m)
    return out

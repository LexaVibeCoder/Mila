# core/llm_client.py
import openai
import json
from typing import List, Dict, Any, Optional, AsyncGenerator
from config import settings, normalize_api_base

class LLMClient:
    """Unified LLM client for Mila.
    
    Can be configured for the main daytime LLM (creative thinking)
    or for night/memory processing with different model settings.
    """
    
    def __init__(self, model: str = None, api_base: str = None, api_key: str = None,
                 max_tokens: int = None, temperature: float = None,
                 top_p: float = None, frequency_penalty: float = None,
                 presence_penalty: float = None):
        base = api_base or settings.LLM_API_BASE
        self.client = openai.AsyncOpenAI(
            base_url=normalize_api_base(base),
            api_key=api_key or settings.LLM_API_KEY
        )
        self.model = model or settings.MAIN_LLM_MODEL
        self.default_max_tokens = max_tokens or settings.MAIN_LLM_MAX_TOKENS
        self.default_temperature = temperature or settings.MAIN_LLM_TEMPERATURE
        self.default_top_p = top_p
        self.default_frequency_penalty = frequency_penalty
        self.default_presence_penalty = presence_penalty

    def reconfigure(self, model: str = None, api_base: str = None, api_key: str = None,
                    max_tokens: int = None, temperature: float = None,
                    top_p: float = None, frequency_penalty: float = None,
                    presence_penalty: float = None):
        """Reconfigure client parameters without re-creating the client.
        
        All parameters are optional — only provided ones are updated.
        This allows changing LLM model/settings on the fly without restarting the bot.
        """
        if model is not None:
            self.model = model
        if api_base is not None:
            self.client = openai.AsyncOpenAI(
                base_url=normalize_api_base(api_base),
                api_key=api_key or settings.LLM_API_KEY
            )
        if api_key is not None:
            self.client = openai.AsyncOpenAI(
                base_url=normalize_api_base(api_base or settings.LLM_API_BASE),
                api_key=api_key
            )
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
        retry_delay: float = 2.0
    ) -> dict:
        """
        Generate response with optional tool calling and system prompt override.
        
        Args:
            messages: Chat history
            tools: OpenAI-format tool definitions (optional)
            tool_choice: "auto", "required", or function name
            max_tokens: Override default max tokens
            temperature: Override default temperature
            system_prompt: Override system prompt by prepending to messages
            max_retries: Number of retries on empty response or transient API errors
            retry_delay: Base delay in seconds between retries (exponential backoff)
        
        Returns:
            dict with: role, content, tool_calls (or None), finish_reason
        """
        import time
        import asyncio
        
        if max_tokens is None:
            max_tokens = self.default_max_tokens
        if temperature is None:
            temperature = self.default_temperature
        
        # Inject system prompt if provided
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages
        
        kwargs = dict(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature
        )
        
        # Pass optional sampling parameters if configured
        if self.default_top_p is not None:
            kwargs["top_p"] = self.default_top_p
        if self.default_frequency_penalty is not None:
            kwargs["frequency_penalty"] = self.default_frequency_penalty
        if self.default_presence_penalty is not None:
            kwargs["presence_penalty"] = self.default_presence_penalty
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                start_time = time.time()
                response = await self.client.chat.completions.create(**kwargs)
                elapsed = time.time() - start_time
                print(f"  [LLM] Response in {elapsed:.2f}s")
            except Exception as e:
                print(f"  [LLM] API Error (attempt {attempt}): {e}")
                last_error = e
                
                if attempt < max_retries:
                    wait = retry_delay * (2 ** (attempt - 1))
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
            
            # Check for empty content when no tool calls
            has_tool_calls = hasattr(message, 'tool_calls') and message.tool_calls
            if not has_tool_calls and not (message.content or "").strip():
                print(f"  [LLM] Empty content (attempt {attempt}), retrying...")
                last_error = Exception("Empty response from LLM API")
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay * (2 ** (attempt - 1)))
                    continue
                else:
                    raise Exception("Empty response from LLM API after all retries")
            
            tool_calls = None
            if has_tool_calls:
                tool_calls = message.tool_calls
            
            return {
                "role": message.role,
                "thoughts": message.content or "",
                "tool_calls": tool_calls,
                "finish_reason": choice.finish_reason
            }
        
        raise last_error or Exception("Empty response from LLM API")

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream response token by token.
        
        For tool calls (think function), yields the full response at the end.
        For regular text responses, yields each token as it arrives.
        
        Yields:
            str: Either a token chunk (for text responses) or a special
                 "__tool_call__:<json>" marker for tool calls.
        """
        if max_tokens is None:
            max_tokens = self.default_max_tokens
        if temperature is None:
            temperature = self.default_temperature

        # Inject system prompt if provided
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages

        kwargs = dict(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": False},
        )

        # Pass optional sampling parameters
        if self.default_top_p is not None:
            kwargs["top_p"] = self.default_top_p
        if self.default_frequency_penalty is not None:
            kwargs["frequency_penalty"] = self.default_frequency_penalty
        if self.default_presence_penalty is not None:
            kwargs["presence_penalty"] = self.default_presence_penalty

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        collected_content = ""
        collected_tool_calls = {}

        try:
            stream = await self.client.chat.completions.create(**kwargs)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Text content
                if delta.content:
                    collected_content += delta.content
                    yield delta.content

                # Tool calls
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in collected_tool_calls:
                            collected_tool_calls[idx] = {
                                "id": "",
                                "function": {"name": "", "arguments": ""}
                            }
                        if tc_delta.id:
                            collected_tool_calls[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                collected_tool_calls[idx]["function"]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                collected_tool_calls[idx]["function"]["arguments"] += tc_delta.function.arguments

            # If we have tool calls, yield them as a special marker
            if collected_tool_calls:
                tool_calls_list = []
                for idx in sorted(collected_tool_calls.keys()):
                    tc = collected_tool_calls[idx]
                    tool_calls_list.append({
                        "index": idx,
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"]
                        }
                    })
                yield f"__tool_call__:{json.dumps(tool_calls_list)}"

        except Exception as e:
            print(f"  [LLM Stream Error] {e}")
            # Fall back to non-streaming if streaming fails
            response = await self.generate(
                messages=messages if not system_prompt else [{"role": "system", "content": system_prompt}] + messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
                system_prompt=None,  # Already prepended
            )
            thoughts = response.get("thoughts", "")
            if thoughts:
                yield thoughts
            tool_calls = response.get("tool_calls")
            if tool_calls:
                tc_list = [
                    {
                        "index": i,
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for i, tc in enumerate(tool_calls)
                ]
                yield f"__tool_call__:{json.dumps(tc_list)}"

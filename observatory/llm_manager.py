"""
LLMManager — manages LLM client instances for Mila.

Provides:
- Dynamic reconfiguration of LLM parameters (model, temperature, etc.)
- Separate instances for main LLM and night LLM
- Test query capability for Model Playground
- Thread-safe singleton pattern

All changes apply immediately without restarting the bot.
"""

import asyncio
import sys
import os
from typing import Any, Dict, Optional, List

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.llm_client import LLMClient
from config import settings, normalize_api_base
from observatory.runtime import runtime_config


class LLMManager:
    """Manages LLM client instances with dynamic reconfiguration."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        # Main LLM client (daytime, creative)
        self._main_client = LLMClient(
            model=settings.MAIN_LLM_MODEL,
            api_base=settings.LLM_API_BASE,
            api_key=settings.LLM_API_KEY,
            max_tokens=settings.MAIN_LLM_MAX_TOKENS,
            temperature=settings.MAIN_LLM_TEMPERATURE,
        )

        # Night LLM client (memory worker)
        night_model = settings.NIGHT_LLM_MODEL or settings.MAIN_LLM_MODEL
        night_api_base = settings.NIGHT_LLM_API_BASE or settings.LLM_API_BASE
        night_api_key = settings.NIGHT_LLM_API_KEY or settings.LLM_API_KEY
        self._night_client = LLMClient(
            model=night_model,
            api_base=night_api_base,
            api_key=night_api_key,
            max_tokens=settings.NIGHT_LLM_MAX_TOKENS,
            temperature=settings.NIGHT_LLM_TEMPERATURE,
        )

        # Track current parameters for display
        self._main_params = {
            "model": settings.MAIN_LLM_MODEL,
            "api_base": settings.LLM_API_BASE,
            "api_key": settings.LLM_API_KEY[:8] + "..." if settings.LLM_API_KEY else "",
            "temperature": settings.MAIN_LLM_TEMPERATURE,
            "max_tokens": settings.MAIN_LLM_MAX_TOKENS,
            "top_p": None,
            "frequency_penalty": None,
            "presence_penalty": None,
        }
        self._night_params = {
            "model": night_model,
            "api_base": night_api_base,
            "api_key": night_api_key[:8] + "..." if night_api_key else "",
            "temperature": settings.NIGHT_LLM_TEMPERATURE,
            "max_tokens": settings.NIGHT_LLM_MAX_TOKENS,
        }

    # ─── Main LLM ────────────────────────────────────────────

    def get_main_client(self) -> LLMClient:
        """Get the main LLM client, applying runtime config overrides."""
        self._apply_runtime_overrides(self._main_client, "llm")
        return self._main_client

    def get_main_params(self) -> Dict[str, Any]:
        """Get current main LLM parameters."""
        self._update_params_from_runtime("llm", self._main_params)
        return dict(self._main_params)

    def configure_main(self, params: Dict[str, Any]) -> None:
        """Reconfigure main LLM client with new parameters."""
        valid_fields = {"model", "api_base", "api_key", "temperature",
                       "max_tokens", "top_p", "frequency_penalty", "presence_penalty"}

        # Store in runtime config
        for key, value in params.items():
            if key in valid_fields:
                runtime_config.set(f"llm.{key}", value)

        # Apply to client
        self._apply_runtime_overrides(self._main_client, "llm")
        self._update_params_from_runtime("llm", self._main_params)
        self._main_client.reconfigure(**params)

    # ─── Night LLM ───────────────────────────────────────────

    def get_night_client(self) -> LLMClient:
        """Get the night LLM client, applying runtime config overrides."""
        self._apply_runtime_overrides(self._night_client, "night_llm")
        return self._night_client

    def get_night_params(self) -> Dict[str, Any]:
        """Get current night LLM parameters."""
        self._update_params_from_runtime("night_llm", self._night_params)
        return dict(self._night_params)

    def configure_night(self, params: Dict[str, Any]) -> None:
        """Reconfigure night LLM client with new parameters."""
        valid_fields = {"model", "api_base", "api_key", "temperature", "max_tokens"}

        for key, value in params.items():
            if key in valid_fields:
                runtime_config.set(f"night_llm.{key}", value)

        self._apply_runtime_overrides(self._night_client, "night_llm")
        self._update_params_from_runtime("night_llm", self._night_params)
        self._night_client.reconfigure(**params)

    # ─── Test Query ──────────────────────────────────────────

    async def test_query(self, prompt: str, client_type: str = "main",
                        params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Send a test query to the LLM (for Model Playground).

        Args:
            prompt: The test prompt to send
            client_type: "main" or "night"
            params: Optional override parameters for this test only

        Returns:
            Dict with response, timing, and model info
        """
        import time

        client = self.get_main_client() if client_type == "main" else self.get_night_client()

        temp = None
        max_tokens = None
        if params:
            temp = params.get("temperature")
            max_tokens = params.get("max_tokens")

        start = time.time()
        try:
            response = await client.generate(
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
                max_tokens=max_tokens or 500,
                max_retries=1,
            )
            elapsed = time.time() - start
            return {
                "success": True,
                "response": response.get("thoughts", ""),
                "timing": f"{elapsed:.2f}s",
                "model": client.model,
                "client_type": client_type,
            }
        except Exception as e:
            elapsed = time.time() - start
            return {
                "success": False,
                "error": str(e),
                "timing": f"{elapsed:.2f}s",
                "model": client.model,
                "client_type": client_type,
            }

    # ─── Internal Helpers ────────────────────────────────────

    def _apply_runtime_overrides(self, client: LLMClient, config_prefix: str) -> None:
        """Apply runtime config overrides to an LLM client."""
        model = runtime_config.get(f"{config_prefix}.model")
        api_base = runtime_config.get(f"{config_prefix}.api_base")
        api_key = runtime_config.get(f"{config_prefix}.api_key")
        temperature = runtime_config.get(f"{config_prefix}.temperature")
        max_tokens = runtime_config.get(f"{config_prefix}.max_tokens")

        overrides = {}
        if model is not None:
            overrides["model"] = model
        if api_base is not None:
            overrides["api_base"] = api_base
        if api_key is not None:
            overrides["api_key"] = api_key
        if temperature is not None:
            overrides["temperature"] = temperature
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens

        if overrides:
            client.reconfigure(**overrides)

    def _update_params_from_runtime(self, config_prefix: str, params: Dict[str, Any]) -> None:
        """Update param dict from runtime config if values are set."""
        for key in params:
            val = runtime_config.get(f"{config_prefix}.{key}")
            if val is not None:
                params[key] = val


# Singleton instance
llm_manager = LLMManager()
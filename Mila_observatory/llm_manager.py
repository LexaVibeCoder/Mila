"""
LLMManager — manages LLM client instances for Mila.

Provides:
- Dynamic reconfiguration of LLM parameters (model, temperature, etc.)
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

from core.llm.client import LLMClient
from config import settings, normalize_api_base
from Mila_observatory.auth import mask_secret, should_keep_existing
from Mila_observatory.runtime import runtime_config


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

        # Main LLM client
        self._main_client = LLMClient(
            model=settings.MAIN_LLM_MODEL,
            api_base=settings.LLM_API_BASE,
            api_key=settings.LLM_API_KEY,
            max_tokens=settings.MAIN_LLM_MAX_TOKENS,
            temperature=settings.MAIN_LLM_TEMPERATURE,
        )

        # Track current parameters for display (api_key masked, never the real key)
        self._main_params = {
            "model": settings.MAIN_LLM_MODEL,
            "api_base": settings.LLM_API_BASE,
            "api_key": mask_secret(settings.LLM_API_KEY),
            "temperature": settings.MAIN_LLM_TEMPERATURE,
            "max_tokens": settings.MAIN_LLM_MAX_TOKENS,
            "top_p": None,
            "frequency_penalty": None,
            "presence_penalty": None,
            "tool_choice": runtime_config.get("llm.tool_choice", "required"),
        }

    # ─── Main LLM ────────────────────────────────────────────

    def get_main_client(self) -> LLMClient:
        """Get the main LLM client, applying runtime config overrides."""
        self._apply_runtime_overrides(self._main_client, "llm")
        return self._main_client

    def get_main_params(self) -> Dict[str, Any]:
        """Get current main LLM parameters (api_key masked)."""
        self._update_params_from_runtime("llm", self._main_params)
        params = dict(self._main_params)
        params["api_key"] = mask_secret(params.get("api_key"))
        return params

    def get_night_params(self) -> Dict[str, Any]:
        """Get current night LLM parameters (runtime config is source of truth)."""
        night = {
            "model": runtime_config.get("night_llm.model"),
            "api_base": runtime_config.get("night_llm.api_base"),
            "api_key": mask_secret(runtime_config.get("night_llm.api_key")),
            "temperature": runtime_config.get("night_llm.temperature"),
            "max_tokens": runtime_config.get("night_llm.max_tokens"),
            "tool_choice": runtime_config.get("night_llm.tool_choice", "required"),
        }
        return night

    def configure_night(self, params: Dict[str, Any]) -> None:
        """Reconfigure night LLM (persisted to runtime config; picked up by
        night worker on next run via auto-reload — no restart needed)."""
        valid_fields = {"model", "api_base", "api_key", "temperature",
                       "max_tokens", "top_p", "frequency_penalty", "presence_penalty",
                       "tool_choice"}
        for key, value in params.items():
            if key in valid_fields:
                if key == "api_key" and should_keep_existing(value):
                    continue  # masked sentinel echoed from GET — keep real key
                runtime_config.set(f"night_llm.{key}", value)

    def configure_main(self, params: Dict[str, Any]) -> None:
        """Reconfigure main LLM client with new parameters."""
        valid_fields = {"model", "api_base", "api_key", "temperature",
                       "max_tokens", "top_p", "frequency_penalty", "presence_penalty",
                       "tool_choice"}

        # Store in runtime config (masked sentinels never overwrite the real key)
        for key, value in params.items():
            if key in valid_fields:
                if key == "api_key" and should_keep_existing(value):
                    continue
                runtime_config.set(f"llm.{key}", value)

        # Apply to client (tool_choice is runtime-only, not an LLMClient field)
        self._apply_runtime_overrides(self._main_client, "llm")
        self._update_params_from_runtime("llm", self._main_params)
        client_params = {k: v for k, v in params.items() if k != "tool_choice"}
        self._main_client.reconfigure(**client_params)

    # ─── Test Query ──────────────────────────────────────────

    async def test_query(self, prompt: str, client_type: str = "main",
                        params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Send a test query to the LLM (for Model Playground).

        Args:
            prompt: The test prompt to send
            client_type: "main" (night is deprecated and falls back to main)
            params: Optional override parameters for this test only

        Returns:
            Dict with response, timing, and model info
        """
        import time

        client = self.get_main_client()

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
        overrides = {}
        for field in ["model", "api_base", "api_key", "temperature",
                       "max_tokens", "top_p", "frequency_penalty", "presence_penalty"]:
            value = runtime_config.get(f"{config_prefix}.{field}")
            if value is not None:
                overrides[field] = value

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

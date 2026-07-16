"""
Mila Runtime Configuration — singleton in memory with JSON persistence.

This is the central dynamic configuration layer for Mila.
Changes to this config apply IMMEDIATELY without restarting the bot.

Architecture:
- Singleton RuntimeConfig object, loaded once at import time
- All changes go through set() methods, which update memory + save to JSON
- Mila reads from this config on every pipeline iteration
- Observatory API writes to this config
"""

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional
from copy import deepcopy


RUNTIME_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "runtime_config.json"
)


class RuntimeConfig:
    """Thread-safe singleton runtime configuration."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()

        # Default values
        self._config: Dict[str, Any] = {
            # LLM
            "llm": {
                "model": None,          # None = use settings.MAIN_LLM_MODEL
                "api_base": None,       # None = use settings.LLM_API_BASE
                "api_key": None,        # None = use settings.LLM_API_KEY
                "temperature": None,    # None = use settings.MAIN_LLM_TEMPERATURE
                "max_tokens": None,     # None = use settings.MAIN_LLM_MAX_TOKENS
                "top_p": None,
                "frequency_penalty": None,
                "presence_penalty": None,
            },
            # Night LLM
            "night_llm": {
                "model": None,
                "api_base": None,
                "api_key": None,
                "temperature": None,
                "max_tokens": None,
            },
            # Tools (enabled/disabled)
            "tools": {
                "search_memory": True,
                "send_message": True,
                "silence": True,
            },
            # Persona
            "persona": {
                "custom_text": "",       # Empty = use default from persona.py
                "parameters": {
                    "empathy": 0.8,
                    "humor": 0.6,
                    "formality": 0.2,
                    "creativity": 0.7,
                }
            },
            # Pipeline
            "pipeline": {
                "max_context_tokens": None,
                "max_think_iterations": 5,
                "decay_lambda": None,
                "forget_threshold": None,
                "similarity_threshold": None,
            },
            # Sleep
            "sleep": {
                "start_hour": None,
                "end_hour": None,
                "forced_awake": False,   # Override sleep mode
            },
            # System
            "system": {
                "observatory_port": 8080,
                "log_level": "INFO",
            },
            # Metadata
            "_meta": {
                "version": 1,
                "last_updated": None,
                "updated_by": "system",
            }
        }

        # Load saved config
        self._load()

    # ─── Public API ──────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dot-notation key: 'llm.model' or 'tools.search_memory'."""
        with self._lock:
            return self._deep_get(self._config, key, default)

    def get_all(self) -> Dict[str, Any]:
        """Get a deep copy of the entire config."""
        with self._lock:
            return deepcopy(self._config)

    def set(self, key: str, value: Any, updated_by: str = "observatory") -> bool:
        """Set a config value by dot-notation key. Returns True if changed."""
        with self._lock:
            keys = key.split(".")
            ptr = self._config
            for k in keys[:-1]:
                if k not in ptr or not isinstance(ptr[k], dict):
                    ptr[k] = {}
                ptr = ptr[k]
            ptr[keys[-1]] = value
            self._config["_meta"]["last_updated"] = datetime.now().isoformat()
            self._config["_meta"]["updated_by"] = updated_by
            self._save()
            return True

    def update(self, updates: Dict[str, Any], prefix: str = "", updated_by: str = "observatory") -> bool:
        """Update multiple values at once. Keys can be dot-notation or nested dict."""
        with self._lock:
            for key, value in updates.items():
                full_key = f"{prefix}.{key}" if prefix else key
                keys = full_key.split(".")
                ptr = self._config
                for k in keys[:-1]:
                    if k not in ptr or not isinstance(ptr[k], dict):
                        ptr[k] = {}
                    ptr = ptr[k]
                ptr[keys[-1]] = value
            self._config["_meta"]["last_updated"] = datetime.now().isoformat()
            self._config["_meta"]["updated_by"] = updated_by
            self._save()
            return True

    def reload(self) -> bool:
        """Reload config from disk. Returns True if successful."""
        with self._lock:
            return self._load()

    # ─── Persistence ─────────────────────────────────────────

    def _load(self) -> bool:
        """Load config from JSON file. Merges with defaults."""
        try:
            os.makedirs(os.path.dirname(RUNTIME_CONFIG_PATH), exist_ok=True)
            if os.path.exists(RUNTIME_CONFIG_PATH):
                with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # Deep merge saved into defaults
                self._deep_merge(self._config, saved)
                return True
        except (json.JSONDecodeError, IOError) as e:
            print(f"[RuntimeConfig] Failed to load config: {e}")
        return False

    def _save(self) -> bool:
        """Save config to JSON file."""
        try:
            os.makedirs(os.path.dirname(RUNTIME_CONFIG_PATH), exist_ok=True)
            with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            return True
        except IOError as e:
            print(f"[RuntimeConfig] Failed to save config: {e}")
            return False

    # ─── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _deep_get(d: dict, key: str, default: Any = None) -> Any:
        """Get a nested value using dot notation."""
        keys = key.split(".")
        ptr = d
        for k in keys:
            if isinstance(ptr, dict) and k in ptr:
                ptr = ptr[k]
            else:
                return default
        return ptr

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        """Deep merge override into base (mutates base)."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                RuntimeConfig._deep_merge(base[key], value)
            else:
                base[key] = value


# Singleton instance
runtime_config = RuntimeConfig()
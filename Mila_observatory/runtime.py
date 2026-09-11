"""
Mila Runtime Configuration — singleton in memory with JSON persistence.

This is the central dynamic configuration layer for Mila.
Changes to this config apply IMMEDIATELY without restarting the bot.

Architecture:
- Singleton RuntimeConfig object, loaded once at import time
- On startup, seeds from config.py (.env) so all values are populated
- All writes go through set()/update() methods, which update memory + save to JSON
- Mila reads from this config on every pipeline iteration (single source of truth)
- Observatory API writes to this config
"""

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional
from copy import deepcopy


# Stored in the project root (not data/) so it persists on the host via the
# docker-compose bind mount (.:/app) instead of the mila_data named volume.
# gitignored because it may contain secrets.
RUNTIME_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "runtime_config.json"
)


class RuntimeConfig:
    """Thread-safe singleton runtime configuration.

    NOTE (docker): bot and observatory run in SEPARATE containers/processes,
    each with its own in-memory copy. The JSON file is the bridge between
    them (shared via bind mount). To make observatory changes visible to
    the bot WITHOUT restart, every read auto-reloads from disk when the
    file mtime changed, and every write re-reads the file first so it
    doesn't clobber the other process's changes.
    """

    _instance = None
    _lock = threading.RLock()

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
        self._lock = threading.RLock()
        self._last_mtime: Optional[float] = None

        # Default values (None = will be seeded from config.py/.env on startup)
        self._config: Dict[str, Any] = {
            # LLM
            "llm": {
                "model": None,
                "api_base": None,
                "api_key": None,
                "temperature": None,
                "max_tokens": None,
                "top_p": None,
                "frequency_penalty": None,
                "presence_penalty": None,
                "tool_choice": "auto",
            },
            # Night LLM
            "night_llm": {
                "model": None,
                "api_base": None,
                "api_key": None,
                "temperature": None,
                "max_tokens": None,
                "tool_choice": "required",
            },
            # Tools (enabled/disabled)
            # silence is legacy (empty content = silence now), kept for compat.
            "tools": {
                "search_memory": True,
                "send_message": True,
                "silence": True,
                "write_diary": True,
                "update_diary": True,
                "presence": True,
                "peek_recent": True,
            },
            # Diary
            "diary": {
                "save_processed": False,
            },
            # Persona
            "persona": {
                "custom_text": "",
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
                "max_think_iterations": 7,
                "decay_lambda": None,
                "forget_threshold": None,
                "similarity_threshold": None,
            },
            # Sleep
            "sleep": {
                "start_hour": None,
                "end_hour": None,
                "forced_awake": False,
            },
            # Proactive (state_agent every N min of global silence)
            "proactive": {
                "enabled": True,
                "idle_minutes": 27,
                "check_interval_seconds": 60,
                "sleep_minutes": 480,
                "cooldown_minutes": 27,
                "max_chats": 20,
                "default_peek_limit": 10,
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

        # Load saved config, then seed remaining None values from .env
        self._load()
        try:
            self._seed_from_settings()
        except ImportError:
            pass  # config.py not available (e.g. minimal install)

    # ─── Public API ──────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dot-notation key: 'llm.model' or 'tools.search_memory'."""
        with self._lock:
            self._maybe_auto_reload()
            return self._deep_get(self._config, key, default)

    def get_all(self) -> Dict[str, Any]:
        """Get a deep copy of the entire config (INCLUDES secrets — internal use only)."""
        with self._lock:
            self._maybe_auto_reload()
            return deepcopy(self._config)

    def get_public_copy(self) -> Dict[str, Any]:
        """Get a deep copy safe for API responses (secrets masked)."""
        from Mila_observatory.auth import mask_secrets_inplace

        with self._lock:
            self._maybe_auto_reload()
            return mask_secrets_inplace(deepcopy(self._config))

    def set(self, key: str, value: Any, updated_by: str = "observatory") -> bool:
        """Set a config value by dot-notation key. Returns True if changed."""
        with self._lock:
            # Re-read the file first: the other container may have written
            # newer values that aren't in our in-memory copy yet. Otherwise
            # we'd overwrite them with stale data on save.
            self._maybe_auto_reload()
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
            self._maybe_auto_reload()
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

    def _maybe_auto_reload(self) -> None:
        """Reload from disk if the file changed since last read.

        Must be called with self._lock held (RLock, so re-entrant).
        Stat failure (file missing) = keep in-memory copy.
        """
        try:
            mtime = os.path.getmtime(RUNTIME_CONFIG_PATH)
        except OSError:
            return
        if self._last_mtime is None or mtime != self._last_mtime:
            self._load()

    def _load(self) -> bool:
        """Load config from JSON file. Merges with defaults."""
        try:
            os.makedirs(os.path.dirname(RUNTIME_CONFIG_PATH), exist_ok=True)
            if os.path.exists(RUNTIME_CONFIG_PATH):
                with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # Deep merge saved into defaults
                self._deep_merge(self._config, saved)
                try:
                    self._last_mtime = os.path.getmtime(RUNTIME_CONFIG_PATH)
                except OSError:
                    pass
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
            try:
                self._last_mtime = os.path.getmtime(RUNTIME_CONFIG_PATH)
            except OSError:
                pass
            return True
        except IOError as e:
            print(f"[RuntimeConfig] Failed to save config: {e}")
            return False

    # ─── Helpers ─────────────────────────────────────────────

    def _seed_from_settings(self) -> None:
        """Fill None values from config.py (.env). Makes runtime_config.json
        the single source of truth with all values resolved on startup."""
        from config import settings

        mapping = {
            "llm.model": "MAIN_LLM_MODEL",
            "llm.api_base": "LLM_API_BASE",
            "llm.api_key": "LLM_API_KEY",
            "llm.temperature": "MAIN_LLM_TEMPERATURE",
            "llm.max_tokens": "MAIN_LLM_MAX_TOKENS",
            "pipeline.max_context_tokens": "MAX_CONTEXT_TOKENS",
            "pipeline.decay_lambda": "DECAY_LAMBDA",
            "pipeline.forget_threshold": "FORGET_THRESHOLD",
            "pipeline.similarity_threshold": "SIMILARITY_THRESHOLD_IMAGE",
        }

        changed = False
        for dot_key, attr_name in mapping.items():
            current = self._deep_get(self._config, dot_key)
            if current is None:
                value = getattr(settings, attr_name, None)
                if value is not None:
                    RuntimeConfig._deep_set(self._config, dot_key, value)
                    changed = True

        # Seed night_llm from main llm values (if still None)
        for field in ["model", "api_base", "api_key"]:
            if self._deep_get(self._config, f"night_llm.{field}") is None:
                llm_val = self._deep_get(self._config, f"llm.{field}")
                if llm_val is not None:
                    RuntimeConfig._deep_set(self._config, f"night_llm.{field}", llm_val)
                    changed = True

        # night_llm temperature/max_tokens hardcoded defaults
        if self._deep_get(self._config, "night_llm.temperature") is None:
            RuntimeConfig._deep_set(self._config, "night_llm.temperature", 0.3)
            changed = True
        if self._deep_get(self._config, "night_llm.max_tokens") is None:
            RuntimeConfig._deep_set(self._config, "night_llm.max_tokens", 4096)
            changed = True

        if changed:
            self._save()

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
    def _deep_set(d: dict, key: str, value: Any) -> None:
        """Set a nested value using dot notation (mutates d in place)."""
        keys = key.split(".")
        ptr = d
        for k in keys[:-1]:
            if k not in ptr or not isinstance(ptr[k], dict):
                ptr[k] = {}
            ptr = ptr[k]
        ptr[keys[-1]] = value

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
"""API keys must come from .env only, never from runtime config."""

import inspect
import json

import Mila_observatory.runtime as runtime_module
from config import get_night_llm_api_key, settings


def _fresh_runtime(monkeypatch, tmp_path, saved):
    """Create an isolated RuntimeConfig singleton backed by tmp_path."""
    path = tmp_path / "runtime_config.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setattr(runtime_module, "RUNTIME_CONFIG_PATH", str(path))
    monkeypatch.setattr(runtime_module.RuntimeConfig, "_instance", None)
    return runtime_module.RuntimeConfig(), path


def test_legacy_api_keys_are_not_read(monkeypatch, tmp_path):
    cfg, _ = _fresh_runtime(
        monkeypatch,
        tmp_path,
        {
            "llm": {"api_key": "legacy-main", "model": "m"},
            "night_llm": {"api_key": "legacy-night", "model": "n"},
        },
    )

    assert cfg.get("llm.api_key") is None
    assert cfg.get("night_llm.api_key") is None
    all_cfg = cfg.get_all()
    assert "api_key" not in all_cfg["llm"]
    assert "api_key" not in all_cfg["night_llm"]


def test_api_key_writes_are_ignored(monkeypatch, tmp_path):
    cfg, path = _fresh_runtime(monkeypatch, tmp_path, {})

    assert cfg.set("llm.api_key", "new-key") is False
    cfg.update({"llm": {"api_key": "new-key"}})
    cfg.set("night_llm.api_key", "new-key")

    assert cfg.get("llm.api_key") is None
    assert cfg.get("night_llm.api_key") is None
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in saved.get("llm", {})
    assert "api_key" not in saved.get("night_llm", {})


def test_night_key_falls_back_to_main(monkeypatch):
    monkeypatch.setattr(settings, "NIGHT_LLM_API_KEY", "")
    assert get_night_llm_api_key() == settings.LLM_API_KEY


def test_night_key_override(monkeypatch):
    monkeypatch.setattr(settings, "NIGHT_LLM_API_KEY", "night-key")
    assert get_night_llm_api_key() == "night-key"


def test_llm_client_cannot_reconfigure_api_key():
    from core.llm.client import LLMClient

    assert "api_key" not in inspect.signature(LLMClient.reconfigure).parameters

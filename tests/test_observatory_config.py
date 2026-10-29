"""Observatory bind address/port resolution (config.py / .env only)."""

from config import settings
from Mila_observatory.auth import resolve_host


def test_resolve_host_defaults_to_loopback(monkeypatch):
    monkeypatch.setattr(settings, "OBSERVATORY_HOST", "")
    assert resolve_host() == "127.0.0.1"


def test_resolve_host_uses_configured_value(monkeypatch):
    monkeypatch.setattr(settings, "OBSERVATORY_HOST", "0.0.0.0")
    assert resolve_host() == "0.0.0.0"


def test_resolve_host_strips_whitespace(monkeypatch):
    monkeypatch.setattr(settings, "OBSERVATORY_HOST", "  192.168.1.10  ")
    assert resolve_host() == "192.168.1.10"


def test_observatory_port_is_an_int_setting():
    assert isinstance(settings.OBSERVATORY_PORT, int)
    assert 0 < settings.OBSERVATORY_PORT < 65536


def test_model_params_accepts_only_known_response_formats():
    import pytest
    from pydantic import ValidationError

    from Mila_observatory.models import ModelParams

    for value in ("json_schema", "json_object", "prompt"):
        assert ModelParams(response_format=value).response_format == value
    with pytest.raises(ValidationError):
        ModelParams(response_format="xml")

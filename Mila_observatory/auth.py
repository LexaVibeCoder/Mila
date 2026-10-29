"""
Mila Observatory — authentication helpers.

- HTTP Basic Auth, credentials from .env (OBSERVATORY_USERNAME / OBSERVATORY_PASSWORD).
- If OBSERVATORY_PASSWORD is empty, auth is disabled: keep the panel
  localhost-only via docker-compose ports ("127.0.0.1:8080:8080").
- Secrets masking: runtime config no longer stores API keys (they come from
  .env only). The helpers below remain as defense-in-depth for legacy files:
  responses never contain full keys, only "abcd1234...wxyz" sentinels, and
  incoming values containing "..." are treated as "leave unchanged".
"""

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)


def is_auth_enabled() -> bool:
    """True when a password is configured (lazy import avoids cycles)."""
    try:
        from config import settings

        return bool(getattr(settings, "OBSERVATORY_PASSWORD", ""))
    except Exception:
        return False


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time credential check against settings."""
    from config import settings

    expected_user = getattr(settings, "OBSERVATORY_USERNAME", "admin") or "admin"
    expected_pass = getattr(settings, "OBSERVATORY_PASSWORD", "") or ""
    if not expected_pass:
        return False
    return secrets.compare_digest(username, expected_user) and secrets.compare_digest(
        password, expected_pass
    )


async def require_auth(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    """FastAPI dependency: no-op when auth disabled, 401 otherwise."""
    if not is_auth_enabled():
        return
    if credentials is None or not verify_credentials(
        credentials.username, credentials.password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Mila Observatory"'},
        )


def resolve_host() -> str:
    """Bind address: explicit OBSERVATORY_HOST wins, otherwise "0.0.0.0".

    NOTE: must stay "0.0.0.0" inside Docker — a container's 127.0.0.1 is
    its own isolated loopback, so a "127.0.0.1:8080:8080" port mapping
    would never reach it. Localhost-only access is enforced one layer
    up, via docker-compose ports ("127.0.0.1:8080:8080").
    """
    from config import settings

    explicit = (getattr(settings, "OBSERVATORY_HOST", "") or "").strip()
    if explicit:
        return explicit
    return "0.0.0.0"


def mask_secret(value: object) -> object:
    """Mask a secret for API responses: 'sk-or-v1-...ab12' style.

    Non-strings are returned unchanged. Short/empty values become "" or "...".
    The "..." substring is the sentinel: such values must never be persisted
    (see should_keep_existing()).
    """
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else value
    if len(value) <= 8:
        return "..."
    return f"{value[:8]}...{value[-4:]}"


def should_keep_existing(value: object) -> bool:
    """True when the incoming value is a masked sentinel → don't overwrite."""
    return isinstance(value, str) and "..." in value


def mask_secrets_inplace(config: dict) -> dict:
    """Mask llm.api_key / night_llm.api_key in a config dict (mutates, returns it)."""
    for section in ("llm", "night_llm"):
        section_cfg = config.get(section)
        if isinstance(section_cfg, dict) and "api_key" in section_cfg:
            section_cfg["api_key"] = mask_secret(section_cfg.get("api_key"))
    return config


def strip_masked_secrets(updates: dict, prefix: str = "") -> dict:
    """Remove masked sentinel values from a flat/dot-notation update dict.

    Prevents "sk-or-v1-...ab12" echoed from GET from overwriting the real key
    on PUT. Handles both {"llm": {"api_key": "…"}} and {"llm.api_key": "…"}.
    """
    cleaned: dict = {}
    for key, value in updates.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            nested = strip_masked_secrets(value, prefix=full_key)
            if nested:
                cleaned[key] = nested
            continue
        if full_key in ("llm.api_key", "night_llm.api_key") and should_keep_existing(
            value
        ):
            continue
        cleaned[key] = value
    return cleaned

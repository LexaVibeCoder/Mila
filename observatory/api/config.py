"""
Config API — view and edit runtime configuration, reload from disk.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException
from observatory.models import ConfigResponse, ConfigUpdateRequest, ReloadResponse
from observatory.runtime import runtime_config, RUNTIME_CONFIG_PATH
from observatory.api.dashboard import add_event

router = APIRouter(tags=["config"])


@router.get("/api/config", response_model=ConfigResponse)
async def get_config():
    """Get full runtime configuration."""
    return ConfigResponse(config=runtime_config.get_all())


@router.put("/api/config", response_model=ConfigResponse)
async def update_config(req: ConfigUpdateRequest):
    """Update runtime configuration values."""
    runtime_config.update(req.updates)
    add_event("config_update", f"Configuration updated: {len(req.updates)} keys")
    return ConfigResponse(config=runtime_config.get_all())


@router.post("/api/config/reload", response_model=ReloadResponse)
async def reload_config():
    """Reload configuration from disk (runtime_config.json)."""
    success = runtime_config.reload()
    reloaded = ["runtime_config.json"]
    add_event("config_reload", "Configuration reloaded from disk")
    return ReloadResponse(
        success=success,
        message="Configuration reloaded from disk" if success else "Failed to reload config",
        reloaded=reloaded,
    )


@router.get("/api/runtime-state", response_model=ConfigResponse)
async def get_runtime_state():
    """
    Get runtime state — combination of runtime config and live system state.
    This is the "Runtime State" page data.
    """
    config = runtime_config.get_all()

    # Add live system state
    config["_live"] = {
        "config_file": RUNTIME_CONFIG_PATH,
        "config_exists": os.path.exists(RUNTIME_CONFIG_PATH),
    }

    return ConfigResponse(config=config)
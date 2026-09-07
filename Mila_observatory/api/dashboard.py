"""
Dashboard API — system status, stats, and recent events.
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter
from Mila_observatory.models import StatusResponse, DashboardResponse, EventItem, PresenceInfo
from Mila_observatory.runtime import runtime_config
from Mila_observatory.llm_manager import llm_manager
from core.db import get_conn as get_mila_conn, get_all_chat_ids_from_chunks

router = APIRouter(tags=["dashboard"])

# Track server start time
SERVER_START_TIME = datetime.now()

# Simple event log (in-memory, recent events only)
_recent_events: list = []


def add_event(event_type: str, description: str):
    """Add an event to the recent events log."""
    _recent_events.append({
        "timestamp": datetime.now().isoformat(),
        "type": event_type,
        "description": description,
    })
    # Keep only last 50 events
    if len(_recent_events) > 50:
        _recent_events.pop(0)


@router.get("/api/status", response_model=StatusResponse)
async def get_status():
    """Get basic system status."""
    uptime = (datetime.now() - SERVER_START_TIME).total_seconds()

    # Count messages and memories
    messages_total = 0
    memories_total = 0
    try:
        conn = get_mila_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM chunks")
        row = cur.fetchone()
        messages_total = row["cnt"] if row else 0

        cur.execute("SELECT COUNT(*) as cnt FROM memories")
        row = cur.fetchone()
        memories_total = row["cnt"] if row else 0
        conn.close()
    except Exception:
        pass

    # Get last activity
    last_activity = None
    try:
        conn = get_mila_conn()
        cur = conn.cursor()
        cur.execute("SELECT MAX(time_start) as last FROM chunks")
        row = cur.fetchone()
        last_activity = row["last"] if row and row["last"] else None
        conn.close()
    except Exception:
        pass

    # Get current model from runtime config
    model = runtime_config.get("llm.model")
    if not model:
        model = llm_manager.get_main_params().get("model", "")

    # Check sleep state
    try:
        from core.sleep.state import get_state
        sleep_state = get_state().value
        is_sleeping = sleep_state == "sleeping" or sleep_state == "committing"
    except Exception:
        is_sleeping = False

    # Current Mila presence (rest/dialog/working/sleep) — never fails the status
    presence = None
    try:
        from core.presence import get_presence
        p = get_presence()
        presence = PresenceInfo(
            state=p.get("state", "rest"),
            focus_chat_id=p.get("focus_chat_id"),
            focus_user_name=p.get("focus_user_name"),
            note=p.get("note", ""),
            updated_at=p.get("updated_at"),
        )
    except Exception:
        presence = None

    return StatusResponse(
        status="online",
        uptime=uptime,
        model=model,
        messages_total=messages_total,
        memories_total=memories_total,
        last_activity=last_activity,
        sleep_mode=is_sleeping,
        runtime_config_version=1,
        presence=presence,
    )


@router.get("/api/presence", response_model=PresenceInfo)
async def get_presence_state():
    """Get Mila's current presence state."""
    try:
        from core.presence import get_presence
        p = get_presence()
        return PresenceInfo(
            state=p.get("state", "rest"),
            focus_chat_id=p.get("focus_chat_id"),
            focus_user_name=p.get("focus_user_name"),
            note=p.get("note", ""),
            updated_at=p.get("updated_at"),
        )
    except Exception:
        return PresenceInfo()


@router.get("/api/events", response_model=list)
async def get_events(limit: int = 10):
    """Get recent system events."""
    return _recent_events[-limit:]


@router.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    """Get full dashboard data."""
    status = await get_status()
    events = _recent_events[-10:]
    return DashboardResponse(
        status=status,
        recent_events=events,
    )
"""
Sleep API — control Mila's sleep/reflection cycle.
"""

import sys
import os
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from Mila_observatory.runtime import runtime_config
from Mila_observatory.api.dashboard import add_event
from core.sleep_manager import (
    start_sleep, wake_up, get_sleep_status, is_sleeping, 
    recover_from_crash, get_pending_messages_count
)

router = APIRouter(tags=["sleep"])


# ─── Request Models ────────────────────────────────────────────

class SleepStartRequest(BaseModel):
    duration_minutes: Optional[int] = Field(None, description="Duration in minutes. None = sleep until manually woken")
    triggered_by: str = Field("manual", description="manual or automatic")


class SleepResponse(BaseModel):
    success: bool
    message: str
    session_id: Optional[str] = None
    duration_minutes: Optional[int] = None


class SleepStatusResponse(BaseModel):
    state: str
    session_id: Optional[str] = None
    current_stage: str = "idle"
    progress_pct: float = 0.0
    started_at: Optional[str] = None
    scheduled_end_at: Optional[str] = None
    completed_at: Optional[str] = None
    triggered_by: str = "manual"
    error_message: Optional[str] = None
    queue_count: int = 0
    elapsed_seconds: Optional[float] = None
    remaining_seconds: Optional[float] = None
    in_progress: bool = False


# ─── Endpoints ─────────────────────────────────────────────────

@router.post("/api/sleep/start", response_model=SleepResponse)
async def api_start_sleep(request: SleepStartRequest):
    """Start a sleep session."""
    if is_sleeping():
        raise HTTPException(status_code=409, detail="Mila is already sleeping")
    
    result = await start_sleep(
        duration_minutes=request.duration_minutes,
        triggered_by=request.triggered_by
    )
    
    if result["success"]:
        add_event("sleep", f"Sleep started (session: {result['session_id'][:8]}...)" + 
                  (f", duration: {request.duration_minutes} min" if request.duration_minutes else ""))
    
    return SleepResponse(
        success=result["success"],
        message=result["message"],
        session_id=result.get("session_id"),
        duration_minutes=request.duration_minutes
    )


@router.post("/api/sleep/wake", response_model=SleepResponse)
async def api_wake_up():
    """Wake Mila up."""
    result = await wake_up()
    
    if result["success"]:
        add_event("wake", "Mila woken up")
    
    return SleepResponse(
        success=result["success"],
        message=result["message"]
    )


@router.get("/api/sleep/status", response_model=SleepStatusResponse)
async def api_sleep_status():
    """Get current sleep status."""
    status = get_sleep_status()
    return SleepStatusResponse(
        state=status.get("state", "awake"),
        session_id=status.get("session_id"),
        current_stage=status.get("current_stage", "idle"),
        progress_pct=status.get("progress_pct", 0.0),
        started_at=status.get("started_at"),
        scheduled_end_at=status.get("scheduled_end_at"),
        completed_at=status.get("completed_at"),
        triggered_by=status.get("triggered_by", "manual"),
        error_message=status.get("error_message"),
        queue_count=status.get("queue_count", 0),
        elapsed_seconds=status.get("elapsed_seconds"),
        remaining_seconds=status.get("remaining_seconds"),
        in_progress=status.get("in_progress", False)
    )


@router.get("/api/sleep/queue-count")
async def api_queue_count():
    """Get count of queued messages."""
    return {"count": get_pending_messages_count()}


@router.post("/api/sleep/config")
async def api_sleep_config(data: dict):
    """Update sleep configuration."""
    valid_keys = {"start_hour", "end_hour", "forced_awake", "save_processed"}
    
    for key, value in data.items():
        if key in valid_keys:
            if key == "save_processed":
                runtime_config.set("diary.save_processed", bool(value))
            else:
                runtime_config.set(f"sleep.{key}", value)
    
    add_event("config", f"Sleep config updated: {data}")
    return {"success": True, "message": "Sleep config updated"}
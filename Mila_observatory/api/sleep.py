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
from core.diary import get_diary_entry_count
from core.sleep_state import get_active_dialogs_detail

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


class SleepEstimateResponse(BaseModel):
    """Estimated sleep duration based on current data volume."""
    diary_entries: int = 0
    active_dialogs: int = 0
    total_messages: int = 0
    estimated_minutes: int = 0
    breakdown: dict = Field(default_factory=dict)


class SleepConfigResponse(BaseModel):
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None
    forced_awake: bool = False
    save_processed: bool = False


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


@router.get("/api/sleep/estimate", response_model=SleepEstimateResponse)
async def api_sleep_estimate():
    """
    Estimate how long sleep/reflection will take based on current data volume.
    
    Calculation:
    - Diary analysis: ~15s per entry (LLM read + think + verify)
    - Dialog summarization: ~20s per active dialog
    - User fact extraction: ~25s per active dialog
    - Overhead: 30s for commit + orchestration
    
    Returns estimated minutes rounded up to nearest minute.
    """
    diary_count = get_diary_entry_count()
    dialogs = get_active_dialogs_detail()
    dialog_count = len(dialogs)
    total_messages = sum(d.get("message_count", 0) for d in dialogs)
    
    # Time estimates in seconds per unit
    DIARY_SEC_PER_ENTRY = 15
    DIALOG_SUMMARIZE_SEC = 20
    FACT_EXTRACT_SEC = 25
    OVERHEAD_SEC = 30
    
    # Diary analysis time
    diary_time = diary_count * DIARY_SEC_PER_ENTRY
    
    # Dialog summarization time
    dialog_time = dialog_count * DIALOG_SUMMARIZE_SEC
    
    # Fact extraction time
    fact_time = dialog_count * FACT_EXTRACT_SEC
    
    total_seconds = diary_time + dialog_time + fact_time + OVERHEAD_SEC
    estimated_minutes = max(1, round(total_seconds / 60))
    
    return SleepEstimateResponse(
        diary_entries=diary_count,
        active_dialogs=dialog_count,
        total_messages=total_messages,
        estimated_minutes=estimated_minutes,
        breakdown={
            "diary_analysis_sec": diary_time,
            "dialog_summarization_sec": dialog_time,
            "fact_extraction_sec": fact_time,
            "overhead_sec": OVERHEAD_SEC,
            "total_sec": total_seconds,
        }
    )


@router.get("/api/sleep/config", response_model=SleepConfigResponse)
async def api_get_sleep_config():
    """Get current sleep configuration from runtime."""
    return SleepConfigResponse(
        start_hour=runtime_config.get("sleep.start_hour"),
        end_hour=runtime_config.get("sleep.end_hour"),
        forced_awake=runtime_config.get("sleep.forced_awake", False),
        save_processed=runtime_config.get("diary.save_processed", False),
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
            elif key == "forced_awake":
                runtime_config.set("sleep.forced_awake", bool(value))
            else:
                runtime_config.set(f"sleep.{key}", value)
    
    add_event("config", f"Sleep config updated: {data}")
    return {"success": True, "message": "Sleep config updated"}
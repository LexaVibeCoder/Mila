"""
State Agent API — endpoints for needs tracking and ML inference.

This API is for testing only and does NOT affect Mila's behavior.
"""

import sys
import os
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter
from pydantic import BaseModel, Field

from state_agent.needs_tracker import get_tracker, ACTIONS

router = APIRouter(tags=["state-agent"])


class ModelInfo(BaseModel):
    loaded: bool
    error: Optional[str] = None
    model_path: str
    num_features: int
    actions: list[str]


class NeedsState(BaseModel):
    seconds_since_last_msg: float
    current_hour: int
    fatigue: float
    need_social: float
    boredom: float
    in_active_dialog: bool
    timestamp: str


class InferenceResult(BaseModel):
    action: str
    probabilities: dict[str, float]
    inference_time_ms: float
    model_loaded: bool
    fallback: bool = False


class StateAgentStatus(BaseModel):
    model: ModelInfo
    needs: NeedsState
    inference: Optional[InferenceResult] = None


class NeedsOverride(BaseModel):
    fatigue: Optional[float] = Field(None, ge=0, le=1)
    need_social: Optional[float] = Field(None, ge=0, le=1)
    boredom: Optional[float] = Field(None, ge=0, le=1)
    seconds_since_last_msg: Optional[float] = Field(None, ge=0)
    current_hour: Optional[int] = Field(None, ge=0, le=23)
    in_active_dialog: Optional[bool] = None


@router.get("/api/state-agent", response_model=StateAgentStatus)
async def get_state_agent_status():
    """Get current state agent status: model info, computed needs, and inference."""
    tracker = get_tracker()
    model_info = tracker.get_model_info()
    needs = tracker.compute_needs()
    inference = tracker.run_inference(needs)

    return StateAgentStatus(
        model=ModelInfo(**model_info),
        needs=NeedsState(**needs),
        inference=InferenceResult(**inference),
    )


@router.post("/api/state-agent/run", response_model=InferenceResult)
async def run_state_agent():
    """Run inference with fresh needs from real data."""
    tracker = get_tracker()
    needs = tracker.compute_needs()
    result = tracker.run_inference(needs)
    return InferenceResult(**result)


@router.post("/api/state-agent/needs", response_model=StateAgentStatus)
async def override_needs(override: NeedsOverride):
    """Override needs manually and run inference with the overridden values."""
    tracker = get_tracker()
    current = tracker.compute_needs()

    if override.fatigue is not None:
        current["fatigue"] = override.fatigue
    if override.need_social is not None:
        current["need_social"] = override.need_social
    if override.boredom is not None:
        current["boredom"] = override.boredom
    if override.seconds_since_last_msg is not None:
        current["seconds_since_last_msg"] = override.seconds_since_last_msg
    if override.current_hour is not None:
        current["current_hour"] = override.current_hour
    if override.in_active_dialog is not None:
        current["in_active_dialog"] = override.in_active_dialog
    current["timestamp"] = datetime.now().isoformat()

    inference = tracker.run_inference(current)

    return StateAgentStatus(
        model=ModelInfo(**tracker.get_model_info()),
        needs=NeedsState(**current),
        inference=InferenceResult(**inference),
    )

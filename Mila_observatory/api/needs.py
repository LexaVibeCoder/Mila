"""
Needs API — endpoints for needs tracking and ML inference.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.needs.tracker import get_tracker, ACTIONS

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
    tracker = get_tracker()
    model_info = tracker.get_model_info()
    needs = tracker.get_needs()
    inference = tracker.run_inference(needs)

    return StateAgentStatus(
        model=ModelInfo(**model_info),
        needs=NeedsState(**needs),
        inference=InferenceResult(**inference),
    )


@router.post("/api/state-agent/run", response_model=InferenceResult)
async def run_state_agent():
    tracker = get_tracker()
    needs = tracker.get_needs(force=True)
    result = tracker.run_inference(needs)
    return InferenceResult(**result)


class TriggerRequest(BaseModel):
    """One-shot proactive run on demand (executed by the bot process).

    - dry_run=True: only describe what WOULD happen (no LLM turn, no send,
      no sleep) — safe simulation of each action branch.
    - force_action: skip inference and route this action through the real
      handler (wait / plan_day / sleep / write_to_user).
    - needs_override: same fields as NeedsOverride, applied to live needs
      before inference (bot side).
    The bot picks the command up within ~10s and reports to last_run.
    """
    dry_run: bool = False
    force_action: Optional[str] = None
    needs_override: Optional[NeedsOverride] = None


class TriggerState(BaseModel):
    pending: bool
    command: Optional[dict] = None
    last_run: Optional[dict] = None


@router.post("/api/state-agent/trigger", response_model=TriggerState)
async def trigger_proactive(req: TriggerRequest):
    from Mila_observatory.runtime import runtime_config
    from Mila_observatory.api.dashboard import add_event
    if req.force_action is not None and req.force_action not in ACTIONS:
        from fastapi import HTTPException
        raise HTTPException(status_code=422,
                            detail=f"Unknown action: {req.force_action}")
    cmd = {
        "run_now": True,
        "dry_run": req.dry_run,
        "force_action": req.force_action,
        "needs_override": (req.needs_override.model_dump(exclude_none=True)
                           if req.needs_override else {}),
        "requested_at": datetime.now().isoformat(),
    }
    runtime_config.set("proactive.command", cmd, updated_by="observatory")
    add_event("proactive_trigger",
              f"Manual trigger (dry={req.dry_run}, action={req.force_action or 'auto'})")
    return TriggerState(
        pending=True,
        command=cmd,
        last_run=runtime_config.get("proactive.last_run"),
    )


@router.get("/api/state-agent/trigger", response_model=TriggerState)
async def get_trigger_state():
    from Mila_observatory.runtime import runtime_config
    cmd = runtime_config.get("proactive.command", {}) or {}
    return TriggerState(
        pending=bool(cmd.get("run_now")),
        command=cmd or None,
        last_run=runtime_config.get("proactive.last_run"),
    )


@router.post("/api/state-agent/needs", response_model=StateAgentStatus)
async def override_needs(override: NeedsOverride):
    tracker = get_tracker()
    current = tracker.get_needs(force=True)

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

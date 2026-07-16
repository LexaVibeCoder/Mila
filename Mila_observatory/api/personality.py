"""
Personality API — view and edit Mila's persona text and parameters.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException
from Mila_observatory.models import PersonaResponse, PersonaUpdateRequest
from Mila_observatory.runtime import runtime_config
from Mila_observatory.api.dashboard import add_event

router = APIRouter(tags=["personality"])

# Path to the persona.py file for reference
PERSONA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "core", "persona.py"
)


@router.get("/api/persona", response_model=PersonaResponse)
async def get_persona():
    """Get current persona settings."""
    custom_text = runtime_config.get("persona.custom_text", "")
    parameters = runtime_config.get("persona.parameters", {})
    return PersonaResponse(
        custom_text=custom_text,
        parameters=parameters,
        using_custom=bool(custom_text),
    )


@router.put("/api/persona", response_model=PersonaResponse)
async def update_persona(req: PersonaUpdateRequest):
    """Update persona settings."""
    if req.custom_text is not None:
        runtime_config.set("persona.custom_text", req.custom_text)

    if req.parameters is not None:
        for key, value in req.parameters.items():
            runtime_config.set(f"persona.parameters.{key}", value)

    add_event("persona_update", "Persona settings updated")

    return await get_persona()


@router.post("/api/persona/reset")
async def reset_persona():
    """Reset persona to default (from persona.py)."""
    runtime_config.set("persona.custom_text", "")
    add_event("persona_reset", "Persona reset to default")
    return await get_persona()
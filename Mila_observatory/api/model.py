"""
Model API — view and control LLM configuration.
"""

import sys
import os
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException
from Mila_observatory.models import (
    ModelParams, ModelResponse, TestQueryRequest, TestQueryResponse
)
from Mila_observatory.llm_manager import llm_manager
from Mila_observatory.api.dashboard import add_event

router = APIRouter(tags=["model"])


@router.get("/api/model", response_model=ModelResponse)
async def get_model_config():
    """Get current LLM model configuration."""
    main_params = llm_manager.get_main_params()
    return ModelResponse(main=main_params)


@router.put("/api/model/main", response_model=ModelResponse)
async def update_main_model(params: ModelParams):
    """Update main LLM configuration (applies immediately)."""
    update_dict = params.model_dump(exclude_none=True)
    if not update_dict:
        raise HTTPException(status_code=400, detail="No parameters to update")

    llm_manager.configure_main(update_dict)

    changed = ", ".join(update_dict.keys())
    add_event("model_change", f"Main LLM updated: {changed}")

    main_params = llm_manager.get_main_params()
    return ModelResponse(main=main_params)


@router.post("/api/model/test", response_model=TestQueryResponse)
async def test_model(req: TestQueryRequest):
    """Send a test query to the LLM (Model Playground)."""
    params = {}
    if req.temperature is not None:
        params["temperature"] = req.temperature
    if req.max_tokens is not None:
        params["max_tokens"] = req.max_tokens

    result = await llm_manager.test_query(
        prompt=req.prompt,
        client_type=req.client_type,
        params=params if params else None,
    )

    add_event("test_query", f"Test query to {req.client_type} LLM: {result['success']}")
    return TestQueryResponse(**result)
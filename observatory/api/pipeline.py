"""
Pipeline API — browse and view pipeline execution logs.
"""

import sys
import os
import json
import glob
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, Query, HTTPException
from observatory.models import (
    PipelineLogItem, PipelineLogListResponse, PipelineLogDetailResponse
)
from config import settings

router = APIRouter(tags=["pipeline"])

LOG_DIR = settings.LOG_DIR


@router.get("/api/pipeline/logs", response_model=PipelineLogListResponse)
async def list_pipeline_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    success_only: Optional[bool] = Query(None, description="Filter by success"),
):
    """List pipeline execution logs."""
    # Find all pipeline log files
    pattern = os.path.join(LOG_DIR, "pipeline_*.json")
    all_files = glob.glob(pattern)
    all_files.sort(reverse=True)  # newest first

    total = len(all_files)
    offset = (page - 1) * limit
    page_files = all_files[offset:offset + limit]

    items = []
    for filepath in page_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Apply filter
            if success_only is not None:
                if data.get("success", True) != success_only:
                    continue

            filename = os.path.basename(filepath)
            timestamp = data.get("timestamp", "")
            user_message = data.get("user_message", "")
            success = data.get("success", True)
            error = data.get("error")
            timing = data.get("timing", {})
            actions = data.get("actions", [])
            iterations = data.get("timing", {}).get("iterations", 0)
            if isinstance(iterations, str):
                try:
                    iterations = int(iterations)
                except (ValueError, TypeError):
                    iterations = 0

            items.append(PipelineLogItem(
                filename=filename,
                timestamp=timestamp,
                user_message=user_message[:200] if user_message else "",
                success=success,
                error=error,
                timing=timing,
                actions_count=len(actions),
                iterations=iterations,
            ))
        except (json.JSONDecodeError, IOError) as e:
            continue

    return PipelineLogListResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/api/pipeline/logs/{filename}", response_model=PipelineLogDetailResponse)
async def get_pipeline_log(filename: str):
    """Get full details of a specific pipeline log."""
    # Sanitize filename to prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = os.path.join(LOG_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Log not found")

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return PipelineLogDetailResponse(filename=filename, data=data)
    except (json.JSONDecodeError, IOError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log: {str(e)}")
"""
Tools API — view and control which tools are enabled/disabled.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException
from Mila_observatory.models import ToolsResponse, ToolStatus, ToolUpdateRequest
from Mila_observatory.runtime import runtime_config
from Mila_observatory.api.dashboard import add_event

router = APIRouter(tags=["tools"])

# Tool descriptions
TOOL_DESCRIPTIONS = {
    "search_memory": "Search long-term memory for relevant information",
    "send_message": "Single brush-off in rest/working only (no dialogue; dialog uses content)",
    "silence": "Legacy: no effect (silence = empty content, no tool)",
    "write_diary": "Write a diary entry (day_entries / reflection_entries)",
    "update_diary": "Update an existing diary entry by its ID",
    "presence": "Presence tools: enter_dialog / enter_rest / enter_working / start_sleep",
    "peek_recent": "Proactive-only: view recent messages across chats at once",
}


@router.get("/api/tools", response_model=ToolsResponse)
async def get_tools():
    """Get list of all tools and their enabled/disabled status."""
    tools_config = runtime_config.get("tools", {})
    tools = []
    for name, enabled in tools_config.items():
        tools.append(ToolStatus(
            name=name,
            enabled=enabled,
            description=TOOL_DESCRIPTIONS.get(name, ""),
        ))
    return ToolsResponse(tools=tools)


@router.put("/api/tools/{tool_name}", response_model=ToolStatus)
async def update_tool(tool_name: str, req: ToolUpdateRequest):
    """Enable or disable a specific tool."""
    valid_tools = {"search_memory", "send_message", "silence", "write_diary", "update_diary", "presence", "peek_recent"}
    if tool_name not in valid_tools:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name}")

    runtime_config.set(f"tools.{tool_name}", req.enabled)

    status = "enabled" if req.enabled else "disabled"
    add_event("tool_change", f"Tool '{tool_name}' {status}")

    return ToolStatus(
        name=tool_name,
        enabled=req.enabled,
        description=TOOL_DESCRIPTIONS.get(tool_name, ""),
    )
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
from Mila_observatory.models import (
    PipelineLogItem, PipelineLogListResponse, PipelineLogDetailResponse,
    PipelineChatItem, PipelineChatsResponse
)
from config import settings

router = APIRouter(tags=["pipeline"])

LOG_DIR = settings.LOG_DIR


def _resolve_display_names(pairs: list) -> dict:
    """Map chat_id -> human-readable name as shown in Telegram.

    Reads chats.chat_name (written by core/queue.py from Telegram chat
    titles / sender names), falling back to users.preferred_name/user_name
    by user_id. Never raises — returns {} if the DB is unavailable.
    """
    try:
        import sqlite3
        db_path = settings.SQLITE_PATH
        if not os.path.isabs(db_path):
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            db_path = os.path.join(root, db_path)
        if not os.path.exists(db_path):
            return {}
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            cur = conn.cursor()
            chat_names = {str(r[0]): (r[1] or "") for r in cur.execute("SELECT chat_id, chat_name FROM chats")}
            user_names: dict = {}
            try:
                for uid, pref, uname in cur.execute("SELECT user_id, preferred_name, user_name FROM users"):
                    user_names[str(uid)] = pref or uname or ""
            except Exception:
                pass
        finally:
            conn.close()
        out: dict = {}
        for chat_id, user_id in pairs:
            name = chat_names.get(str(chat_id), "") or ""
            if not name and user_id:
                name = user_names.get(str(user_id), "") or ""
            out[str(chat_id)] = name
        return out
    except Exception:
        return {}


@router.get("/api/pipeline/logs", response_model=PipelineLogListResponse)
async def list_pipeline_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    success_only: Optional[bool] = Query(None, description="Filter by success"),
    chat_id: Optional[str] = Query(None, description="Filter by chat_id"),
):
    """List pipeline execution logs."""
    # Find all pipeline log files
    pattern = os.path.join(LOG_DIR, "pipeline_*.json")
    all_files = glob.glob(pattern)
    all_files.sort(reverse=True)  # newest first

    items_all = []
    for filepath in all_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Apply filters
            if success_only is not None:
                if data.get("success", True) != success_only:
                    continue

            file_chat_id = str(data.get("chat_id", "") or "")
            if chat_id is not None and chat_id != "":
                if file_chat_id != chat_id:
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

            items_all.append({
                "filename": filename,
                "timestamp": timestamp,
                "user_message": user_message[:200] if user_message else "",
                "chat_id": file_chat_id,
                "user_id": str(data.get("user_id", "") or ""),
                "success": success,
                "error": error,
                "timing": timing,
                "actions_count": len(actions),
                "iterations": iterations,
            })
        except (json.JSONDecodeError, IOError) as e:
            continue

    # Resolve human-readable chat names (as shown in Telegram) in one DB pass
    display_names = _resolve_display_names(
        [(it["chat_id"], it["user_id"]) for it in items_all]
    )
    items_page = []
    for it in items_all:
        it["display_name"] = display_names.get(it["chat_id"], "")
        items_page.append(PipelineLogItem(**it))

    total = len(items_page)
    offset = (page - 1) * limit
    items = items_page[offset:offset + limit]

    return PipelineLogListResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/api/pipeline/chats", response_model=PipelineChatsResponse)
async def list_pipeline_chats():
    """List chats that have pipeline logs, with message counts."""
    pattern = os.path.join(LOG_DIR, "pipeline_*.json")
    all_files = glob.glob(pattern)
    all_files.sort(reverse=True)  # newest first

    chats: dict[str, dict] = {}
    for filepath in all_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            file_chat_id = str(data.get("chat_id", "") or "")
            if not file_chat_id:
                file_chat_id = "(unknown)"
            timestamp = str(data.get("timestamp", "") or "")
            user_message = str(data.get("user_message", "") or "")
            user_id = str(data.get("user_id", "") or "")
            entry = chats.get(file_chat_id)
            if entry is None:
                chats[file_chat_id] = {
                    "chat_id": file_chat_id,
                    "user_id": user_id,
                    "count": 1,
                    "last_timestamp": timestamp,
                    "last_message": user_message[:120],
                }
            else:
                entry["count"] += 1
                # files are sorted newest first, so first seen is the latest
                if not entry.get("user_id") and user_id:
                    entry["user_id"] = user_id
        except (json.JSONDecodeError, IOError):
            continue

    items = [PipelineChatItem(**v) for v in chats.values()]
    # newest first
    items.sort(key=lambda x: x.last_timestamp, reverse=True)
    # Resolve human-readable chat names (as shown in Telegram) in one DB pass
    display_names = _resolve_display_names(
        [(c.chat_id, c.user_id) for c in items]
    )
    for c in items:
        c.display_name = display_names.get(c.chat_id, "")
    return PipelineChatsResponse(items=items, total=len(items))


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
        _sanitize_multimodal_messages(data)
        return PipelineLogDetailResponse(filename=filename, data=data)
    except (json.JSONDecodeError, IOError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log: {str(e)}")


def _sanitize_content(content):
    """Render multimodal message content as plain text for the UI.

    Native vision/audio parts are stored in logs as OpenAI content blocks
    (lists with {type, ...}). The UI needs no preview — just short labels.
    Base64 payloads are dropped (they can be megabytes).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
                continue
            if not isinstance(p, dict):
                continue
            t = p.get("type", "")
            if t == "text":
                parts.append(str(p.get("text", "")))
            elif t == "image_url":
                parts.append("[фото]")
            elif t == "input_audio":
                parts.append("[голосовое сообщение]")
            elif t:
                parts.append(f"[{t}]")
        return "\n".join(x for x in parts if x)
    return str(content)


def _sanitize_multimodal_messages(data: dict) -> None:
    """Replace multimodal llm_messages content with plain-text labels, in place."""
    try:
        messages = data.get("llm_messages")
        if not isinstance(messages, list):
            return
        for m in messages:
            if isinstance(m, dict) and "content" in m:
                m["content"] = _sanitize_content(m.get("content"))
    except Exception:
        pass
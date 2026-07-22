"""
Diary API — просмотр и управление личными дневниками Милы.

Эндпоинты:
- GET /api/diary — список всех доступных дневников
- GET /api/diary/today — содержимое дневника за сегодня
- GET /api/diary/{date} — содержимое дневника за конкретную дату
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from core.diary.manager import (
    load_daily_report,
    list_daily_reports,
    get_diary_markdown,
    get_diary_entry_count,
)

router = APIRouter(tags=["diary"])


class DiarySummary(BaseModel):
    """Summary info for a single diary entry."""
    date: str
    entries: int
    preview: str = ""


class DiaryDetail(BaseModel):
    """Full diary content."""
    date: str
    content: str
    entry_count: int


@router.get("/api/diary")
async def get_all_diaries():
    """Get list of all available daily reports with summaries."""
    dates = list_daily_reports()
    
    result = []
    for date_str in dates:
        content = load_daily_report(date_str)
        # Count entries in the markdown (lines starting with "## Запись #")
        entry_count = content.count("## Запись #") if content else 0
        # Get first 200 chars as preview
        preview_lines = content.split("\n") if content else []
        preview = ""
        for line in preview_lines:
            if line.strip() and not line.startswith("#"):
                preview = line.strip()[:200]
                break
        
        result.append({
            "date": date_str,
            "entries": entry_count,
            "preview": preview
        })
    
    return {
        "reports": result,
        "total": len(result)
    }


@router.get("/api/diary/today")
async def get_today_diary():
    """Get today's diary content."""
    content = load_daily_report()  # No date = today
    if not content:
        return {
            "date": "today",
            "content": "",
            "entry_count": 0,
            "message": "За сегодня записей в дневнике нет"
        }
    
    entry_count = content.count("## Запись #")
    return {
        "date": "today",
        "content": content,
        "entry_count": entry_count
    }


@router.get("/api/diary/{date}")
async def get_diary_by_date(date: str):
    """
    Get diary content for a specific date.
    
    Date format: DD.MM.YYYY (e.g., 17.07.2026)
    """
    # Validate date format
    import re
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", date):
        raise HTTPException(
            status_code=400,
            detail=f"Неверный формат даты: '{date}'. Используйте ДД.ММ.ГГГГ (например, 17.07.2026)"
        )
    
    content = load_daily_report(date)
    if not content:
        raise HTTPException(
            status_code=404,
            detail=f"Дневник за {date} не найден"
        )
    
    from core.diary.manager import DAILY_REPORTS_DIR
    filepath = os.path.join(DAILY_REPORTS_DIR, f"{date}.md")
    file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    entry_count = content.count("## Запись #")
    
    return {
        "date": date,
        "content": content,
        "entry_count": entry_count,
        "file_size_bytes": file_size,
        "file_path": filepath
    }
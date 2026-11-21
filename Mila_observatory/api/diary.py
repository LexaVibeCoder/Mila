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

from core.diary.manager import (
    load_daily_report,
    list_daily_reports,
    count_report_entries,
    daily_report_path,
    parse_report_date,
    today_report_date,
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
        entry_count = count_report_entries(content)
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
    date_str = today_report_date()
    content = load_daily_report(date_str)
    if not content:
        return {
            "date": date_str,
            "content": "",
            "entry_count": 0,
            "message": "За сегодня записей в дневнике нет"
        }
    
    entry_count = count_report_entries(content)
    return {
        "date": date_str,
        "content": content,
        "entry_count": entry_count
    }


@router.get("/api/diary/{date}")
async def get_diary_by_date(date: str):
    """
    Get diary content for a specific date.
    
    Date format: DD.MM.YYYY (e.g., 17.07.2026)
    """
    try:
        parse_report_date(date)
    except ValueError:
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
    
    filepath = daily_report_path(date)
    file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    entry_count = count_report_entries(content)
    
    return {
        "date": date,
        "content": content,
        "entry_count": entry_count,
        "file_size_bytes": file_size,
        "file_path": filepath
    }

"""
core/model_sleeper.py — Sleep/wake mechanism for Mila.

Controls whether the bot is "sleeping" (not processing messages) or awake.
The night pipeline sets the sleep flag; the bot checks it before each message.

The flag is stored in the main mila.db in a `sleep_flags` table.
"""

from datetime import datetime
from typing import Optional

from config import settings
from core.db import get_conn


FLAG_NAME = "mila_sleeping"


def _ensure_flag_table():
    """Ensure the sleep_flags table exists."""
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sleep_flags (
                flag_name TEXT PRIMARY KEY,
                flag_value TEXT NOT NULL DEFAULT '0'
            )
        """)
        conn.commit()
    finally:
        conn.close()


def is_sleeping() -> bool:
    """Check if the bot is currently in sleep mode."""
    _ensure_flag_table()
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT flag_value FROM sleep_flags WHERE flag_name=?",
            (FLAG_NAME,)
        )
        row = cur.fetchone()
        return row is not None and row["flag_value"] == "1"
    finally:
        conn.close()


def set_sleep_flag():
    """Set the bot to sleep mode."""
    _ensure_flag_table()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sleep_flags (flag_name, flag_value) VALUES (?, '1')",
            (FLAG_NAME,)
        )
        conn.commit()
        print("[Sleeper] Sleep flag SET — bot will not process messages.")
    finally:
        conn.close()


def clear_sleep_flag():
    """Clear the sleep flag (wake the bot)."""
    _ensure_flag_table()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sleep_flags (flag_name, flag_value) VALUES (?, '0')",
            (FLAG_NAME,)
        )
        conn.commit()
        print("[Sleeper] Sleep flag CLEARED — bot is awake.")
    finally:
        conn.close()


def should_sleep_now(start_hour: int = None, end_hour: int = None) -> bool:
    """
    Check if the current time falls within the sleep window.
    
    Args:
        start_hour: Hour when sleep starts (default from settings.SLEEP_START_HOUR)
        end_hour: Hour when sleep ends (default from settings.SLEEP_END_HOUR)
    
    Returns:
        True if current time is within [start_hour, end_hour) range,
        handling overnight ranges (start > end).
    """
    if start_hour is None:
        start_hour = settings.SLEEP_START_HOUR
    if end_hour is None:
        end_hour = settings.SLEEP_END_HOUR
    
    now = datetime.now()
    current_hour = now.hour
    
    if start_hour <= end_hour:
        # Normal range: e.g., 10:00 - 18:00
        return start_hour <= current_hour < end_hour
    else:
        # Overnight range: e.g., 23:00 - 7:00
        return current_hour >= start_hour or current_hour < end_hour


def time_until_sleep_end(start_hour: int = None, end_hour: int = None) -> float:
    """
    Calculate seconds until sleep window ends.
    Useful for the night pipeline to know how long to wait.
    """
    if start_hour is None:
        start_hour = settings.SLEEP_START_HOUR
    if end_hour is None:
        end_hour = settings.SLEEP_END_HOUR
    
    now = datetime.now()
    
    # Calculate target wake time: today at end_hour, or tomorrow if end_hour <= current hour
    wake_time = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    
    if wake_time <= now:
        # Wake time already passed today → tomorrow
        wake_time = wake_time.replace(day=wake_time.day + 1)
    
    delta = (wake_time - now).total_seconds()
    return max(0, delta)
"""Global action journal — where Mila was and what she did.

The journal is stored in ``mila.db`` (same WAL database as presence/sleep) and
rendered into the system prompt as a compact timeline. It answers one question:
"what was I doing today?" — so a turn can see its own recent state history
across chats, not just the current chat.

Deliberately *not* journaled:
- incoming notifications (they are already in the chat context);
- ``search_memory`` results (only needed for the current reply);
- ``write_diary`` / ``update_diary`` (the diary itself is the record);
- ``peek_recent_chats`` (a read-only look at other chats);
- ordinary dialog replies (only ``send_message`` is an explicit outbound note).

State changes are recorded from every source (model tools, auto-rest,
auto-sleep, Observatory/manual, crash recovery). Sleep is a special case: the
start and wake events carry the real duration, which is the strongest source
of the feeling of time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from core.db import get_conn

# Keep the prompt bounded. Older lines are dropped from the *display*, not
# from the database.
JOURNAL_MAX_LINES = 60
# Sleep can last up to 12h; 36h also covers a state span crossing midnight.
JOURNAL_LOOKBACK_HOURS = 36
SEND_TEXT_LIMIT = 200

_JOURNAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS action_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT,
    prev_state TEXT,
    chat_id TEXT,
    chat_name TEXT,
    chat_alias TEXT,
    prev_chat_id TEXT,
    prev_chat_name TEXT,
    prev_chat_alias TEXT,
    prev_duration_seconds REAL,
    note TEXT DEFAULT '',
    text TEXT DEFAULT '',
    meta TEXT DEFAULT ''
)
"""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _local_zone():
    """Configured IANA timezone, falling back to the OS local zone."""
    if getattr(settings, "LOCAL_TIMEZONE", ""):
        try:
            return ZoneInfo(settings.LOCAL_TIMEZONE)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clock(dt: datetime) -> str:
    return dt.astimezone(_local_zone()).strftime("%H:%M")


def _full_time(dt: datetime) -> str:
    return dt.astimezone(_local_zone()).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: Any) -> str:
    """Human-readable duration for the prompt (Russian)."""
    if seconds is None:
        return ""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        seconds = 0
    total_minutes = int(seconds // 60)
    if total_minutes < 1:
        return "меньше минуты"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def _format_minutes(minutes: Any) -> str:
    if minutes is None:
        return ""
    try:
        return _format_duration(float(minutes) * 60)
    except (TypeError, ValueError):
        return ""


def _truncate_text(text: Any, limit: int = SEND_TEXT_LIMIT) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Chat labels
# ---------------------------------------------------------------------------

_PLACEHOLDER_NAMES = {"", "без имени", "unknown", "личный чат"}


def _clean_name(name: str | None) -> str | None:
    value = (name or "").strip()
    return None if value.casefold() in _PLACEHOLDER_NAMES else value


def _chat_fields(chat_id: str | None, fallback_name: str | None = None):
    """Return (chat_id, display_name, alias) without exposing numeric IDs."""
    if not chat_id:
        return None, None, None
    cid = str(chat_id)
    alias = None
    name = _clean_name(fallback_name)
    try:
        from core.db import get_chat_alias, get_chat_display_name
        alias = get_chat_alias(cid)
        if not name:
            name = _clean_name(get_chat_display_name(cid))
    except Exception:
        pass
    return cid, name, alias


def _chat_label(alias: str | None, name: str | None) -> str:
    """Alias (Name), or whichever is available. Never a numeric chat id."""
    alias = (alias or "").strip()
    name = _clean_name(name)
    if alias and name and alias.casefold() != name.casefold():
        return f"{alias} ({name})"
    return alias or name or "неизвестный чат"


def _chat_short(alias: str | None, name: str | None) -> str:
    return (alias or "").strip() or _clean_name(name) or "неизвестный чат"


def _prev_place_label(state: str | None, alias: str | None, name: str | None) -> str:
    """Where Mila came from: ``из rest`` / ``из иван`` / ``из сна``."""
    state = state or ""
    if state == "dialog":
        return _chat_short(alias, name)
    if state == "rest":
        return "rest"
    if state == "working":
        return "working"
    if state == "sleep":
        return "сна"
    return state or "неизвестного состояния"


def _prev_state_label(state: str | None, alias: str | None, name: str | None) -> str:
    """Used after ``до этого была в ...``: ``в rest`` / ``в чате иван``."""
    state = state or ""
    if state == "dialog":
        chat = _chat_short(alias, name)
        return f"чате {chat}" if chat != "неизвестный чат" else "диалоге"
    if state == "sleep":
        return "сне"
    return state or "неизвестном состоянии"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute(_JOURNAL_TABLE_SQL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_journal_created "
            "ON action_journal(created_at)"
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _insert(row: dict, now: datetime | None = None) -> int:
    created_at = _as_utc(now).isoformat()
    conn = get_conn()
    try:
        _ensure_table(conn)
        cur = conn.execute(
            "INSERT INTO action_journal "
            "(created_at, kind, state, prev_state, chat_id, chat_name, chat_alias, "
            " prev_chat_id, prev_chat_name, prev_chat_alias, prev_duration_seconds, "
            " note, text, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                created_at,
                row.get("kind") or "",
                row.get("state"),
                row.get("prev_state"),
                row.get("chat_id"),
                row.get("chat_name"),
                row.get("chat_alias"),
                row.get("prev_chat_id"),
                row.get("prev_chat_name"),
                row.get("prev_chat_alias"),
                row.get("prev_duration_seconds"),
                row.get("note") or "",
                row.get("text") or "",
                json.dumps(row.get("meta") or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _row_to_entry(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["_dt"] = _parse_utc(entry.get("created_at"))
    try:
        entry["meta"] = json.loads(entry.get("meta") or "{}")
    except (ValueError, TypeError):
        entry["meta"] = {}
    return entry


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_state_change(
    prev: dict | None,
    new_state: str,
    *,
    chat_id: str | None = None,
    chat_name: str | None = None,
    note: str = "",
    now: datetime | None = None,
    source: str = "presence",
) -> str | None:
    """Record a rest/dialog/working transition. Returns the fresh event line.

    ``prev`` is the presence snapshot *before* the switch. It carries the
    previous ``updated_at`` so the event can say how long the previous state
    lasted.

    Same-state/same-chat transitions are ignored: they are not a change of
    place, and journaling them would reset the duration of the current span.
    """
    prev = prev or {}
    new_state = str(new_state or "rest")
    now_utc = _as_utc(now)
    prev_state = prev.get("state") or "rest"
    prev_chat_id = prev.get("focus_chat_id")
    prev_chat_name = prev.get("focus_user_name")

    if prev_state == new_state and str(prev_chat_id or "") == str(chat_id or ""):
        return None

    prev_updated = _parse_utc(prev.get("updated_at"))
    prev_duration = None
    if prev_updated is not None:
        prev_duration = max(0.0, (now_utc - prev_updated).total_seconds())

    cid, cname, calias = _chat_fields(chat_id, chat_name)
    pcid, pcname, pcalias = _chat_fields(prev_chat_id, prev_chat_name)

    row = {
        "kind": "state",
        "state": new_state,
        "prev_state": prev_state,
        "chat_id": cid,
        "chat_name": cname,
        "chat_alias": calias,
        "prev_chat_id": pcid,
        "prev_chat_name": pcname,
        "prev_chat_alias": pcalias,
        "prev_duration_seconds": prev_duration,
        "note": note,
        "meta": {"source": source},
    }
    _insert(row, now=now_utc)
    return _fresh_state_event(row, now_utc)


def record_sleep_start(
    duration_minutes: int | None,
    prev: dict | None,
    *,
    note: str = "",
    now: datetime | None = None,
    source: str = "sleep",
) -> str | None:
    """Record going to sleep, with the planned duration."""
    prev = prev or {}
    now_utc = _as_utc(now)
    prev_state = prev.get("state") or "rest"
    prev_chat_id = prev.get("focus_chat_id")
    prev_chat_name = prev.get("focus_user_name")
    prev_updated = _parse_utc(prev.get("updated_at"))
    prev_duration = None
    if prev_updated is not None:
        prev_duration = max(0.0, (now_utc - prev_updated).total_seconds())

    pcid, pcname, pcalias = _chat_fields(prev_chat_id, prev_chat_name)
    row = {
        "kind": "state",
        "state": "sleep",
        "prev_state": prev_state,
        "prev_chat_id": pcid,
        "prev_chat_name": pcname,
        "prev_chat_alias": pcalias,
        "prev_duration_seconds": prev_duration,
        "note": note,
        "meta": {
            "source": source,
            "duration_minutes": duration_minutes,
        },
    }
    _insert(row, now=now_utc)
    return _fresh_sleep_start(row, now_utc)


def record_sleep_wake(
    duration_seconds: float | None,
    *,
    note: str = "",
    now: datetime | None = None,
    source: str = "sleep",
) -> str | None:
    """Record waking up. A wake is also a state boundary (back to rest)."""
    now_utc = _as_utc(now)
    row = {
        "kind": "wake",
        "state": "rest",
        "prev_state": "sleep",
        "note": note,
        "meta": {
            "source": source,
            "duration_seconds": duration_seconds,
        },
    }
    _insert(row, now=now_utc)
    return _fresh_wake_event(row, now_utc)


def record_send_message(
    chat_id: str,
    text: str,
    *,
    now: datetime | None = None,
    source: str = "send_message",
) -> str | None:
    """Record one explicit outbound note (``send_message`` tool only)."""
    cid, cname, calias = _chat_fields(chat_id)
    row = {
        "kind": "send_message",
        "chat_id": cid,
        "chat_name": cname,
        "chat_alias": calias,
        "text": _truncate_text(text),
        "meta": {"source": source},
    }
    _insert(row, now=now)
    return _fresh_send_event(row, now)


# ---------------------------------------------------------------------------
# Fresh event lines (shown at the bottom of the current turn)
# ---------------------------------------------------------------------------

def _fresh_state_event(row: dict, now: datetime | None = None) -> str:
    ts = _full_time(_as_utc(now))
    state = row.get("state") or ""
    prev_state = row.get("prev_state") or ""
    note = (row.get("note") or "").strip()
    prev_place = _prev_place_label(
        prev_state, row.get("prev_chat_alias"), row.get("prev_chat_name"))
    prev_state_label = _prev_state_label(
        prev_state, row.get("prev_chat_alias"), row.get("prev_chat_name"))
    duration = _format_duration(row.get("prev_duration_seconds"))

    if state == "dialog":
        target = _chat_label(row.get("chat_alias"), row.get("chat_name"))
        line = f"[{ts}] ты зашла в чат {target}"
        if prev_state:
            line += f" из {prev_place}"
        if duration and prev_state:
            line += f", до этого была в {prev_state_label} {duration}"
    elif state == "rest":
        line = f"[{ts}] ты перешла в rest"
    elif state == "working":
        line = f"[{ts}] ты перешла в working"
    elif state == "sleep":
        line = f"[{ts}] ты легла спать"
    else:
        line = f"[{ts}] ты перешла в {state or 'неизвестное состояние'}"

    if note:
        line += f". заметка: {note}"
    return line


def _fresh_sleep_start(row: dict, now: datetime | None = None) -> str:
    ts = _full_time(_as_utc(now))
    duration = _format_minutes((row.get("meta") or {}).get("duration_minutes"))
    line = f"[{ts}] ты легла спать"
    if duration:
        line += f" на {duration}"
    note = (row.get("note") or "").strip()
    if note:
        line += f". заметка: {note}"
    return line


def _fresh_wake_event(row: dict, now: datetime | None = None) -> str:
    ts = _full_time(_as_utc(now))
    duration = _format_duration((row.get("meta") or {}).get("duration_seconds"))
    line = f"[{ts}] ты проснулась"
    if duration:
        line += f", спала {duration}"
    note = (row.get("note") or "").strip()
    if note:
        line += f". заметка: {note}"
    return line


def _fresh_send_event(row: dict, now: datetime | None = None) -> str:
    ts = _full_time(_as_utc(now))
    chat = _chat_label(row.get("chat_alias"), row.get("chat_name"))
    text = row.get("text") or ""
    return f"[{ts}] ты отправила в чат {chat}: «{text}»"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _next_boundary(entries: list[dict], index: int) -> dict | None:
    for entry in entries[index + 1:]:
        if entry.get("kind") in ("state", "wake"):
            return entry
    return None


def _render_entry(entries: list[dict], index: int, now_utc: datetime,
                  day_start_utc: datetime) -> str | None:
    entry = entries[index]
    dt = entry.get("_dt")
    if dt is None:
        return None
    kind = entry.get("kind")
    note = (entry.get("note") or "").strip()
    meta = entry.get("meta") or {}
    # The window includes a sleep/dialog tail from yesterday; mark it so it is
    # not confused with today's 23:30.
    prefix = "вчера " if dt < day_start_utc else ""

    if kind == "send_message":
        chat = _chat_label(entry.get("chat_alias"), entry.get("chat_name"))
        return f"[{prefix}{_clock(dt)}] отправила в чат {chat}: «{entry.get('text') or ''}»"

    if kind == "wake":
        duration = _format_duration(meta.get("duration_seconds"))
        line = f"[{prefix}{_clock(dt)}] проснулась"
        if duration:
            line += f", спала {duration}"
        if note:
            line += f" ({note})"
        return line

    if kind != "state":
        return None

    state = entry.get("state") or ""
    if state == "dialog":
        chat = _chat_short(entry.get("chat_alias"), entry.get("chat_name"))
        nxt = _next_boundary(entries, index)
        if nxt is not None and nxt.get("_dt") is not None:
            end_clock = _clock(nxt["_dt"])
            line = f"[{prefix}{_clock(dt)}–{end_clock}] была в чате {chat}"
        else:
            line = f"[{prefix}{_clock(dt)}–сейчас] в чате {chat}"
        if note:
            line += f" — {note}"
        return line

    if state == "rest":
        line = f"[{prefix}{_clock(dt)}] перешла в rest"
    elif state == "working":
        line = f"[{prefix}{_clock(dt)}] перешла в working"
    elif state == "sleep":
        duration = _format_minutes(meta.get("duration_minutes"))
        line = f"[{prefix}{_clock(dt)}] легла спать"
        if duration:
            line += f" на {duration}"
    else:
        line = f"[{prefix}{_clock(dt)}] состояние: {state or 'неизвестное'}"

    if note:
        line += f" ({note})"
    return line


def get_journal_context(
    now: datetime | None = None,
    *,
    max_lines: int = JOURNAL_MAX_LINES,
    lookback_hours: int = JOURNAL_LOOKBACK_HOURS,
) -> str:
    """Render the compact "what I did today" block for the system prompt.

    Window: local midnight → now, plus the tail of a sleep / dialog span that
    started before midnight. Returns an empty string when there is nothing
    useful to show.
    """
    now_utc = _as_utc(now)
    local_now = now_utc.astimezone(_local_zone())
    day_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    lookback_utc = day_start_utc - timedelta(hours=max(0, int(lookback_hours)))

    conn = get_conn()
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT * FROM action_journal WHERE created_at >= ? "
            "ORDER BY created_at ASC, id ASC",
            (lookback_utc.isoformat(),),
        ).fetchall()
    finally:
        conn.close()

    entries = [_row_to_entry(row) for row in rows]
    entries = [e for e in entries if e.get("_dt") is not None]
    if not entries:
        return ""

    today = [e for e in entries if e["_dt"] >= day_start_utc]
    before = [e for e in entries if e["_dt"] < day_start_utc]

    state_before = None
    for entry in reversed(before):
        if entry.get("kind") in ("state", "wake"):
            state_before = entry
            break

    selected: list[dict] = []
    if today:
        # Carry the tail of a sleep or an open dialog across midnight.
        if state_before and state_before.get("kind") == "state" \
                and state_before.get("state") in ("sleep", "dialog"):
            selected.append(state_before)
        selected.extend(today)
    else:
        # Nothing today: show the current ongoing location, not yesterday's
        # rest line.
        if state_before and state_before.get("kind") == "state" \
                and state_before.get("state") in ("dialog", "working", "sleep"):
            selected.append(state_before)

    if not selected:
        return ""

    lines = []
    for index, entry in enumerate(selected):
        line = _render_entry(selected, index, now_utc, day_start_utc)
        if line:
            lines.append(line)

    if not lines:
        return ""
    if max_lines and len(lines) > max_lines:
        lines = ["…"] + lines[-max_lines:]
    return "\n".join(lines)

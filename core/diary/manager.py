"""SQLite diary, local-day reports and sleep backups."""

import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from tempfile import NamedTemporaryFile
from threading import Lock
from uuid import uuid4

from config import project_path, settings
from core.context import buffer

DIARY_TABLE = "diary_entries"
DIARY_CATEGORY_LABELS = {
    "day_entries": "Day Events",
    "reflection_entries": "Thoughts and Feelings",
}
DIARY_CATEGORIES = tuple(DIARY_CATEGORY_LABELS)
MAX_ENTRY_LENGTH = 1200
DAILY_CATEGORY_LIMIT = 3
DAILY_REPORTS_DIR = project_path(settings.DAILY_REPORTS)
REPORT_DATE_FORMAT = "%d.%m.%Y"
_ENTRY_HEADING = re.compile(r"^## (?:Entry|Запись) #\d+\b", re.MULTILINE)
_INIT_LOCK = Lock()


def normalize_category(category) -> str:
    """Use the day category for unrecognized tool input."""
    return category if category in DIARY_CATEGORIES else "day_entries"


def today_report_date() -> str:
    """Return today's local date in the report filename format."""
    return buffer.now_local().strftime(REPORT_DATE_FORMAT)


def parse_report_date(value: str) -> date:
    """Parse a canonical report date."""
    day = datetime.strptime(value, REPORT_DATE_FORMAT).date()
    if day.strftime(REPORT_DATE_FORMAT) != value:
        raise ValueError("Invalid daily report date")
    return day


def daily_report_path(date_str: str) -> str:
    """Resolve the file for a validated local date."""
    parse_report_date(date_str)
    return os.path.join(DAILY_REPORTS_DIR, f"{date_str}.md")


def count_report_entries(markdown: str) -> int:
    """Count entry headings in current and legacy daily reports."""
    return len(_ENTRY_HEADING.findall(markdown))


def _utc_day_bounds(day: date) -> tuple[str, str]:
    zone = buffer._local_zone()
    return tuple(
        datetime.combine(boundary, time.min, tzinfo=zone)
        .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for boundary in (day, day + timedelta(days=1))
    )


def _entry_day(entry: dict) -> date:
    local = buffer.format_created_at_local(entry["created_at"])
    return date.fromisoformat(local[:10])


def init_diary() -> None:
    """Initialize the diary at startup, once per context database in this process."""
    with _INIT_LOCK:
        _init_diary(os.path.abspath(buffer.CONTEXT_DB_PATH))


@lru_cache(maxsize=1)
def _init_diary(db_path: str) -> None:
    with closing(buffer._get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {DIARY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_number INTEGER NOT NULL,
                content_md TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'day_entries',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                archived INTEGER NOT NULL DEFAULT 0
            )
        """)
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({DIARY_TABLE})")}
        if "category" not in columns:
            conn.execute(f"ALTER TABLE {DIARY_TABLE} ADD COLUMN category TEXT NOT NULL DEFAULT 'day_entries'")
        if "updated_at" not in columns:
            conn.execute(f"ALTER TABLE {DIARY_TABLE} ADD COLUMN updated_at TEXT")
            conn.execute(f"UPDATE {DIARY_TABLE} SET updated_at=created_at")
        if "archived" not in columns:
            conn.execute(f"ALTER TABLE {DIARY_TABLE} ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        conn.execute(f"CREATE INDEX IF NOT EXISTS diary_by_date ON {DIARY_TABLE} (created_at)")


def _connect():
    init_diary()
    return buffer._get_conn()


def _entries(conn, category=None, local_day=None, include_archived=False) -> list[dict]:
    conditions = ["archived=0"] if not include_archived else []
    args = []
    if category is not None:
        conditions.append("category=?")
        args.append(normalize_category(category))
    if local_day is not None:
        conditions.append("created_at >= ? AND created_at < ?")
        args.extend(_utc_day_bounds(local_day))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"SELECT id, entry_number, content_md, category, created_at, updated_at "
        f"FROM {DIARY_TABLE}{where} ORDER BY created_at, id", args
    )
    return [dict(row) for row in rows]


def get_all_diary_entries(category: str = None, local_day: date = None) -> list[dict]:
    """Read active entries oldest first, optionally filtered by local day."""
    with closing(_connect()) as conn:
        return _entries(conn, category, local_day)


def _atomic_write(filepath: str, markdown: str) -> None:
    directory = os.path.dirname(filepath)
    os.makedirs(directory, exist_ok=True)
    file = NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False)
    try:
        with file:
            file.write(markdown)
            file.flush()
            os.fsync(file.fileno())
        os.replace(file.name, filepath)
    finally:
        if os.path.exists(file.name):
            os.unlink(file.name)


def _sync_to_daily_file(day: date) -> bool:
    date_str = day.strftime(REPORT_DATE_FORMAT)
    try:
        # Serialize the snapshot and atomic replacement with other diary writes.
        with closing(_connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            entries = _entries(conn, local_day=day, include_archived=True)
            lines = [f"# Personal Diary — {date_str}", ""]
            for entry in entries:
                created = buffer.format_created_at_local(entry["created_at"])[:16]
                lines.extend([
                    f"## Entry #{entry['entry_number']} — {entry['category']} — {created} (ID: {entry['id']})",
                    "", entry["content_md"], "", "---", "",
                ])
            _atomic_write(daily_report_path(date_str), "\n".join(lines))
        return True
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[Diary] Failed to sync daily file: {type(exc).__name__}")
        return False


def validate_entry_text(text: str) -> str | None:
    """Return a tool-friendly validation error for an invalid entry."""
    if not text.strip():
        return "Missing diary entry text (param 'entry')."
    if len(text) > MAX_ENTRY_LENGTH:
        return f"Diary entries may contain up to {MAX_ENTRY_LENGTH} characters."
    return None


def _result(entry, status: str, **details) -> dict:
    return {"status": status, "entry_id": entry["id"], "entry_number": entry["entry_number"],
            "category": entry["category"], **details}


def _duplicate(conn, text: str, day: date, exclude_id: int | None = None):
    return conn.execute(
        f"SELECT id, entry_number, category, archived, created_at FROM {DIARY_TABLE} "
        "WHERE TRIM(content_md, ' ' || CHAR(9, 10, 13)) = ? AND id IS NOT ? "
        "AND (archived=0 OR (created_at >= ? AND created_at < ?)) "
        "ORDER BY archived, id LIMIT 1",
        (text, exclude_id, *_utc_day_bounds(day)),
    ).fetchone()


def _duplicate_result(entry) -> dict:
    return _result(entry, "duplicate", message=f"В записи дневника с ID {entry['id']} уже есть этот текст.")


def _recent_candidates(conn, category: str, exclude_id: int | None) -> list[dict]:
    rows = conn.execute(
        f"SELECT id, content_md FROM {DIARY_TABLE} WHERE archived=0 AND category=? AND id IS NOT ? "
        "ORDER BY created_at DESC, id DESC LIMIT 6", (category, exclude_id),
    )
    return [dict(row) for row in rows]


def diary_review_context(
    text: str, category: str | None, *, entry_id: int | None = None,
) -> tuple[dict | None, list[dict]] | None:
    """Read the edit target and recent active candidates, skipping locally resolved requests."""
    with closing(_connect()) as conn:
        conn.execute("BEGIN")
        target = None
        day = buffer.now_local().date()
        if entry_id is not None:
            row = conn.execute(
                f"SELECT id, category, content_md, created_at FROM {DIARY_TABLE} WHERE id=? AND archived=0",
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            target = dict(row)
            day = _entry_day(target)
            if category is None:
                category = target["category"]
            if text == target["content_md"] and normalize_category(category) == target["category"]:
                return None
        if _duplicate(conn, text, day, exclude_id=entry_id) is not None:
            return None
        return target, _recent_candidates(conn, normalize_category(category), entry_id)


def _current_candidate(conn, category: str, candidate: dict):
    return conn.execute(
        f"SELECT * FROM {DIARY_TABLE} WHERE id=? AND archived=0 AND category=? AND content_md=?",
        (candidate["id"], category, candidate["content_md"]),
    ).fetchone()


def _review_stale() -> dict:
    return {"status": "review_stale", "error": "Дневник изменился во время смысловой проверки."}


def _category_limit(conn, day: date, category: str) -> dict | None:
    entries = _entries(conn, category, day, include_archived=True)
    if len(entries) < DAILY_CATEGORY_LIMIT:
        return None
    return {
        "status": "limit_reached",
        "error": f"Daily limit of {DAILY_CATEGORY_LIMIT} entries in {category} reached.",
    }


def _replace_entry(conn, entry, text: str, category: str) -> dict:
    if text != entry["content_md"] or category != entry["category"]:
        conn.execute(
            f"UPDATE {DIARY_TABLE} SET content_md=?, category=?, updated_at=datetime('now') WHERE id=?",
            (text, category, entry["id"]),
        )
    return _result(entry, "updated", category=category)


def add_diary_entry(
    content_md: str, category: str = "day_entries", *, match=None, candidates: list[dict] | None = None,
) -> dict:
    """Save a draft with its validated semantic decision, if available."""
    category = normalize_category(category)
    text = content_md.strip()
    if error := validate_entry_text(text):
        return {"status": "error", "error": error}

    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        now = buffer.now_local()
        day = now.date()
        duplicate = _duplicate(conn, text, day)
        if duplicate is not None:
            result = _duplicate_result(duplicate)
            day = _entry_day(duplicate)
        elif match is not None:
            status, candidate = match
            current = _current_candidate(conn, category, candidate)
            if current is None:
                return _review_stale()
            if status == "duplicate":
                return _result(current, status, message="Смысл черновика уже есть в дневнике.")
            if status == "update_suggested":
                return _result(current, status, update_entry_id=current["id"],
                               existing_entry=current["content_md"])
            day = _entry_day(current)
            result = _replace_entry(conn, current, text, category)
        else:
            if candidates is not None and candidates != _recent_candidates(conn, category, None):
                return _review_stale()
            if limit := _category_limit(conn, day, category):
                return limit

            number = conn.execute(
                f"SELECT COALESCE(MAX(entry_number), 0) + 1 FROM {DIARY_TABLE}"
            ).fetchone()[0]
            utc = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                f"INSERT INTO {DIARY_TABLE} (entry_number, content_md, category, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (number, text, category, utc, utc)
            )
            result = _result({"id": cur.lastrowid, "entry_number": number, "category": category}, "written")

    # A repeat of a committed write can repair a failed Markdown export.
    return {**result, "daily_file_synced": _sync_to_daily_file(day)}


def update_diary_entry(
    entry_id: int, content_md: str, category: str | None = None, *,
    expected: dict | None = None, match=None, candidates: list[dict] | None = None,
) -> dict:
    """Replace an active entry and regenerate the report for its original day."""
    text = content_md.strip()
    if error := validate_entry_text(text):
        return {"status": "error", "error": error}

    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        entry = conn.execute(
            f"SELECT * FROM {DIARY_TABLE} WHERE id=? AND archived=0", (entry_id,)
        ).fetchone()
        if entry is None:
            return {"status": "not_found", "entry_id": entry_id, "error": f"Diary entry ID {entry_id} not found."}
        if expected is not None and (entry["category"], entry["content_md"]) != (
            expected["category"], expected["content_md"],
        ):
            return _review_stale()
        day = _entry_day(entry)
        duplicate = _duplicate(conn, text, day, exclude_id=entry_id)
        if duplicate is not None:
            return _duplicate_result(duplicate)
        target_category = entry["category"] if category is None else normalize_category(category)
        if match is not None:
            status, candidate = match
            if status == "unchanged":
                return _result(entry, status, message="Смысл записи не изменился.")
            current = _current_candidate(conn, target_category, candidate)
            if current is None:
                return _review_stale()
            return _result(current, "duplicate", message="Смысл черновика уже есть в другой записи дневника.")
        if candidates is not None and candidates != _recent_candidates(conn, target_category, entry_id):
            return _review_stale()
        if target_category != entry["category"]:
            if limit := _category_limit(conn, day, target_category):
                return limit
        result = _replace_entry(conn, entry, text, target_category)
    return {**result, "daily_file_synced": _sync_to_daily_file(day)}


def _diary_markdown(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = ["# Diary", ""]
    for entry in entries:
        lines.extend([
            f"[{entry['entry_number']:03d}] ({entry['category']}, ID: {entry['id']})",
            entry["content_md"], "",
        ])
    return "\n".join(lines)


def get_diary_markdown() -> str:
    """Render all active entries for sleep analysis."""
    return get_diary_entries_markdown()


def get_diary_entries_markdown(category: str = None) -> str:
    """Render active entries with IDs, optionally filtered by category."""
    return _diary_markdown(get_all_diary_entries(category))


def get_diary_entry_count() -> int:
    """Count active entries awaiting sleep analysis."""
    with closing(_connect()) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {DIARY_TABLE} WHERE archived=0").fetchone()[0]


def snapshot_diary_entries() -> list[dict]:
    """Capture active rows for the sleep transaction's undo record."""
    return get_all_diary_entries()


def restore_diary_entries(rows: list[dict]) -> int:
    """Restore archived or legacy deleted rows without overwriting active rows."""
    if not rows:
        return 0
    with closing(_connect()) as conn, conn:
        cur = conn.executemany(
            f"INSERT INTO {DIARY_TABLE} (id, entry_number, content_md, category, created_at, updated_at) "
            "VALUES (:id, :entry_number, :content_md, :category, :created_at, :updated_at) "
            "ON CONFLICT(id) DO UPDATE SET archived=0 WHERE archived=1",
            (
                {
                    **row,
                    "content_md": row.get("content_md") or "",
                    "category": row.get("category") or "day_entries",
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
                for row in rows
            ),
        )
        return cur.rowcount


def save_diary_to_logs(entries: list[dict] | None = None) -> bool:
    """Write a separate backup of the supplied or current active entries."""
    if entries is None:
        entries = get_all_diary_entries()
    markdown = _diary_markdown(entries)
    if not markdown:
        return True

    filename = f"diary_{buffer.now_local():%Y%m%d}_{uuid4().hex}.md"
    filepath = os.path.join(project_path(settings.LOG_DIR), "diary_logs", filename)
    try:
        _atomic_write(filepath, markdown)
        return True
    except OSError as exc:
        print(f"[Diary] Failed to save log: {type(exc).__name__}")
        return False


def clear_diary(expected_entries: list[dict] | None = None) -> None:
    """Back up and archive the active diary, optionally guarding a staged snapshot."""
    entries = snapshot_diary_entries() if expected_entries is None else expected_entries
    if not save_diary_to_logs(entries):
        raise OSError("Diary backup failed")

    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if _entries(conn) != entries:
            raise RuntimeError("Diary changed while preparing the backup")
        conn.execute(f"UPDATE {DIARY_TABLE} SET archived=1 WHERE archived=0")


def load_daily_report(date_str: str = None) -> str:
    """Read a daily report by local date."""
    try:
        with open(daily_report_path(date_str or today_report_date()), encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        return ""


def list_daily_reports() -> list[str]:
    """List reports by calendar date, newest first."""
    try:
        filenames = os.listdir(DAILY_REPORTS_DIR)
    except FileNotFoundError:
        return []
    dates = []
    for filename in filenames:
        if filename.endswith(".md"):
            try:
                dates.append(parse_report_date(filename[:-3]))
            except ValueError:
                continue
    return [day.strftime(REPORT_DATE_FORMAT) for day in sorted(dates, reverse=True)]


def get_today_diary_context(max_entries: int = 30) -> str:
    """Format today's active entries for the system prompt, with IDs for updates."""
    if max_entries <= 0:
        return ""
    entries = get_all_diary_entries(local_day=buffer.now_local().date())[-max_entries:]
    blocks = []
    for category, label in DIARY_CATEGORY_LABELS.items():
        lines = [f"[ID: {entry['id']}] {entry['content_md']}" for entry in entries if entry["category"] == category]
        if lines:
            blocks.append("\n".join([f"## {label} ({category}):", *lines]))
    if not blocks:
        return ""
    return "\n".join([
        "", "[Your personal diary for today. Each entry has an ID — use update_diary to update an existing entry:]",
        "---", *blocks, "---", "",
    ])

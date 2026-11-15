"""Context buffer summary storage (TODO.md S8, S9, S12; restore used by S2/S3).

The buffer is the destructive side of a sleep summary: these tests pin the
storage contract the sleep rollback relies on.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.context import buffer

CHAT = "chat-1"
FIXED_UTC = datetime(2026, 1, 1, 12, 3, 40, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_UTC if tz is None else FIXED_UTC.astimezone(tz)


@pytest.fixture
def local_ahead_of_utc(monkeypatch):
    """Frozen clock at 12:03:40 UTC, configured local zone at a fixed +09:00 offset."""
    monkeypatch.setattr(buffer, "datetime", _FrozenDatetime)
    monkeypatch.setattr(buffer, "_local_zone", lambda: timezone(timedelta(hours=9)))


def _messages(chat=CHAT):
    return [r for r in buffer.get_context(chat, limit=100000) if r["type"] == "message"]


def _summaries(chat=CHAT):
    return [r for r in buffer.get_context(chat, limit=100000) if r["type"] == "summary"]


def _snapshot(rows):
    return [(r["id"], r["content"], r["type"]) for r in rows]


def _seed(n, chat=CHAT):
    for i in range(n):
        buffer.push_message(chat, f"alpha message {i}")
    return _messages(chat)


# ---------------------------------------------------------------------------
# S12: summary timestamps are UTC, not the local wall clock
# ---------------------------------------------------------------------------

def test_push_summary_created_at_is_utc(sleep_env, local_ahead_of_utc):
    buffer.push_summary(CHAT, "alpha summary")

    rows = _summaries()
    assert [r["created_at"] for r in rows] == ["2026-01-01 12:03:40"]


def test_compressed_summary_created_at_is_utc(sleep_env, local_ahead_of_utc):
    rows = _seed(3)
    buffer.compress_messages_into_summary(CHAT, "alpha summary", [rows[0]["id"]])

    assert [r["created_at"] for r in _summaries()] == ["2026-01-01 12:03:40"]


def test_push_message_created_at_is_utc(sleep_env, local_ahead_of_utc):
    buffer.push_message(CHAT, "alpha message")

    assert [r["created_at"] for r in _messages()] == ["2026-01-01 12:03:40"]


def test_message_display_time_is_rendered_in_local_zone(sleep_env, local_ahead_of_utc):
    buffer.push_message(CHAT, "alpha message")

    stored = [r["created_at"] for r in _messages()]
    assert len(stored) == 1
    # Storage stays UTC; only the display edge converts to the configured zone (+09:00 here).
    assert buffer.format_created_at_local(stored[0]) == "2026-01-01 21:03:40"


def test_summary_timestamp_tracks_the_real_utc_clock(sleep_env):
    before = datetime.now(timezone.utc).replace(tzinfo=None)

    buffer.push_summary(CHAT, "alpha summary")

    after = datetime.now(timezone.utc).replace(tzinfo=None)
    stored = datetime.strptime(_summaries()[0]["created_at"], "%Y-%m-%d %H:%M:%S")
    assert before - timedelta(seconds=2) <= stored <= after + timedelta(seconds=2)


# ---------------------------------------------------------------------------
# S9: removed count is the real rowcount
# ---------------------------------------------------------------------------

def test_compress_returns_real_deleted_count_and_ignores_unknown_ids(sleep_env):
    rows = _seed(5)
    ids = [rows[0]["id"], rows[1]["id"], 999999]

    summary_id, removed = buffer.compress_messages_into_summary(CHAT, "alpha summary", ids)

    assert removed == 2
    assert [m["content"] for m in _messages()] == [
        "alpha message 2", "alpha message 3", "alpha message 4"]
    summaries = _summaries()
    assert [s["id"] for s in summaries] == [summary_id]


def test_compress_never_deletes_existing_summaries(sleep_env):
    rows = _seed(3)
    first_id, removed = buffer.compress_messages_into_summary(
        CHAT, "alpha summary one", [rows[0]["id"]])
    assert removed == 1

    _second_id, removed = buffer.compress_messages_into_summary(
        CHAT, "alpha summary two", [first_id])

    assert removed == 0
    assert len(_summaries()) == 2


# ---------------------------------------------------------------------------
# S8: push + pop is all-or-nothing
# ---------------------------------------------------------------------------

def test_failed_removal_leaves_no_summary_and_retry_does_not_duplicate(
        sleep_env, context_delete_failures):
    rows = _seed(5)
    before = _snapshot(_messages())
    ids = [r["id"] for r in rows]

    context_delete_failures["on"] = True
    with pytest.raises(sqlite3.OperationalError):
        buffer.compress_messages_into_summary(CHAT, "alpha summary", ids)
    context_delete_failures["on"] = False

    assert _summaries() == []
    assert _snapshot(_messages()) == before

    summary_id, removed = buffer.compress_messages_into_summary(CHAT, "alpha summary", ids)

    assert removed == 5
    assert [s["id"] for s in _summaries()] == [summary_id]
    assert _messages() == []


# ---------------------------------------------------------------------------
# S2 helper: restoring deleted rows is idempotent and keeps original ids
# ---------------------------------------------------------------------------

def test_restore_messages_is_idempotent_and_keeps_original_ids(sleep_env):
    rows = _seed(3)
    original = _snapshot(rows)
    deleted_rows = buffer.peek_oldest_messages_by_tokens(CHAT, 10**9)
    buffer.compress_messages_into_summary(CHAT, "alpha summary", [r["id"] for r in rows])
    assert _messages() == []

    first = buffer.restore_messages(CHAT, deleted_rows)
    second = buffer.restore_messages(CHAT, deleted_rows)

    assert first == 3
    assert second == 0
    assert _snapshot(_messages()) == original

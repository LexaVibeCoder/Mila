"""Hermetic test bootstrap.

Everything here runs BEFORE any project module is imported, so `config.py`
never sees the developer's real `.env` or the process environment. Tests must
work on a clean checkout: no secrets, no network, no real `data/`.
"""

import os
import tempfile
import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mila-tests-"))

# Required by config.Settings; values are intentionally fake.
_DUMMY_ENV = {
    "API_ID": "1",
    "API_HASH": "test-api-hash",
    "PHONE_NUMBER": "",
    "LLM_API_BASE": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "test-key",
    "MAIN_LLM_MODEL": "test-model",
    "EMBED_API_BASE": "http://127.0.0.1:9/v1",
    "EMBED_API_KEY": "test-key",
    "EMBED_MODEL": "test-embed",
    # Never touch the real TDLib session or runtime files.
    "TD_FILES_DIRECTORY": str(_TMP_ROOT / "tdlib"),
    "SQLITE_PATH": str(_TMP_ROOT / "mila.db"),
    "CONTEXT_DB_PATH": str(_TMP_ROOT / "context_buffer.db"),
    "FAISS_TEXT_PATH": str(_TMP_ROOT / "faiss_text.index"),
    "FAISS_CHUNK_PATH": str(_TMP_ROOT / "faiss_chunks.index"),
    "FAISS_IMAGE_PATH": str(_TMP_ROOT / "faiss_image.index"),
    "PHOTOS_DIR": str(_TMP_ROOT / "photos"),
    "MEDIA_DIR": str(_TMP_ROOT / "media"),
    "DAILY_REPORTS": str(_TMP_ROOT / "daily reports"),
    "LOG_DIR": str(_TMP_ROOT / "logs"),
    "RERANKER_MODEL_PATH": str(_TMP_ROOT / "reranker"),
    "LOCAL_TIMEZONE": "UTC",
    # Never read/write the developer's real runtime_config.json.
    "MILA_RUNTIME_CONFIG_PATH": str(_TMP_ROOT / "runtime_config.json"),
}
# Force overrides: a real exported variable must not leak into tests.
os.environ.update(_DUMMY_ENV)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Remove the session temp dir unless a test asked to keep it."""
    if os.getenv("MILA_TEST_KEEP_TMP") != "1":
        import shutil

        shutil.rmtree(_TMP_ROOT, ignore_errors=True)


# ---------------------------------------------------------------------------
# Sleep test harness (shared by tests/test_sleep_*.py and
# tests/test_context_buffer_summary.py). Abstract data only: user-1, chat-1,
# "alpha fact", "alpha summary". No LLM, no embeddings, no FAISS, no network.
# ---------------------------------------------------------------------------

CHAT_ID = "chat-1"
USER_ID = "user-1"


def _fake_count_tokens(text, model=None):  # noqa: ARG001
    """Deterministic token counter. tiktoken may download BPE data; tests must not."""
    return max(1, len(text or "") // 4)


class FakeVectorIndex:
    def __init__(self):
        self.removed_ids = []

    def remove_ids(self, ids):
        self.removed_ids.extend(int(i) for i in ids)


class FakeIndexer:
    """Stands in for MemoryIndexer: writes the real memories row, skips embedding/FAISS."""

    def __init__(self):
        self.vector = FakeVectorIndex()
        self.calls = []
        # When True, the row is inserted and then the call "crashes" (S15 window).
        self.crash_after_insert = False

    async def add_memory(self, user_id, content, importance=0.5, tags=None,
                         source_chat_id=None, source_message_id=None,
                         faiss_id=None, mem_id=None):
        from core import db as mila_db

        mem_id = mem_id or str(uuid.uuid4())
        self.calls.append({"mem_id": mem_id, "user_id": user_id, "content": content})
        mila_db.add_memory(
            user_id=user_id, content=content, importance=importance, tags=tags,
            source_chat_id=source_chat_id, source_message_id=source_message_id,
            faiss_id=faiss_id, mem_id=mem_id,
        )
        if self.crash_after_insert:
            raise RuntimeError("simulated crash after memory insert")
        return mem_id, True


class SleepEnv:
    chat_id = CHAT_ID
    user_id = USER_ID

    def __init__(self, tmp_path, indexer):
        self.tmp_path = tmp_path
        self.indexer = indexer
        self.media_dir = tmp_path / "media"
        self.log_dir = tmp_path / "logs"

    # ---- seeding -------------------------------------------------------
    def seed_messages(self, count, prefix="alpha message", chat_id=CHAT_ID,
                      source_chunk_ids=None):
        from core.context import buffer

        for i in range(count):
            chunk = None
            if source_chunk_ids and i < len(source_chunk_ids):
                chunk = source_chunk_ids[i]
            buffer.push_message(chat_id, f"{prefix} {i}", source_chunk_id=chunk)
        return self.messages(chat_id)

    def add_diary(self, text):
        from core.diary.manager import add_diary_entry

        return add_diary_entry(text)["entry_id"]

    # ---- reads ---------------------------------------------------------
    def context_rows(self, chat_id=CHAT_ID):
        from core.context import buffer

        return buffer.get_context(chat_id, limit=100000)

    def messages(self, chat_id=CHAT_ID):
        return [r for r in self.context_rows(chat_id) if r["type"] == "message"]

    def summaries(self, chat_id=CHAT_ID):
        return [r for r in self.context_rows(chat_id) if r["type"] == "summary"]

    def diary_count(self):
        from core.diary.manager import get_diary_entry_count

        return get_diary_entry_count()

    def memory_count(self, content):
        rows = self._rows("SELECT COUNT(*) AS n FROM memories WHERE content=?", (content,))
        return rows[0]["n"]

    def staging(self, session_id):
        return self._rows(
            "SELECT * FROM sleep_transaction_staging WHERE session_id=? ORDER BY id",
            (session_id,))

    def summary_log(self, session_id):
        return self._rows(
            "SELECT * FROM sleep_dialog_summary_log WHERE session_id=? ORDER BY id",
            (session_id,))

    def _rows(self, sql, params=()):
        from core import db as mila_db
        from core.sleep.state import _ensure_sleep_tables

        _ensure_sleep_tables()
        conn = mila_db.get_conn()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    # ---- sleep helpers ---------------------------------------------------
    def begin_sleep(self, session_id=None):
        from core.sleep.state import SleepState, set_state

        session_id = session_id or str(uuid.uuid4())
        set_state(SleepState.SLEEPING, session_id=session_id, triggered_by="test")
        return session_id

    def call(self, name, **args):
        """Fake LLM tool call with the same shape as the OpenAI SDK objects."""
        return SimpleNamespace(function=SimpleNamespace(
            name=name, arguments=json.dumps(args, ensure_ascii=False)))

    async def stage_summary(self, session_id, text="alpha summary", chat_id=CHAT_ID,
                            claimed=None):
        from core.sleep.processes import StagingExecutor

        executor = StagingExecutor(session_id, "dialog_summarization")
        args = {"chat_id": chat_id, "summary_text": text}
        if claimed is not None:
            args["message_count"] = claimed
        return await executor.execute_tool_call(self.call("save_dialog_summary", **args))

    async def stage_fact(self, session_id, content="alpha fact", user_id=USER_ID,
                         importance=0.7):
        from core.sleep.processes import StagingExecutor

        executor = StagingExecutor(
            session_id, "user_fact_extraction",
            known_user_ids={USER_ID}, default_source_chat_id=CHAT_ID)
        return await executor.execute_tool_call(self.call(
            "add_user_fact", user_id=user_id, content=content, importance=importance))

    def use_processes(self, monkeypatch, work, ok=True):
        """Replace the LLM-driven process fan-out with a deterministic writer.

        ``work(session_id)`` is an async callable that stages changes through the
        real StagingExecutor; ``ok`` is the verdict the sleep manager sees.
        """
        import core.sleep.manager as sleep_manager

        async def fake_run(session_id):
            await work(session_id)
            status = "All processes completed" if ok else "Some processes failed"
            return ok, status, ["fake process"]

        monkeypatch.setattr(sleep_manager, "run_all_processes", fake_run)


@pytest.fixture
def sleep_env(tmp_path, monkeypatch):
    """Temp SQLite DBs, temp media/logs/daily dirs, fake indexer, no presence I/O."""
    from config import settings
    from core import db as mila_db
    from core.context import buffer
    from core.diary import manager as diary
    from core.memory import indexer as indexer_module
    import core.sleep.manager as sleep_manager

    monkeypatch.setattr(mila_db, "DB_PATH", str(tmp_path / "mila.db"))
    monkeypatch.setattr(buffer, "CONTEXT_DB_PATH", str(tmp_path / "context_buffer.db"))
    monkeypatch.setattr(buffer, "count_tokens", _fake_count_tokens)
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(diary, "DAILY_REPORTS_DIR", str(tmp_path / "daily"))
    # Presence/journal are best-effort side channels; keep them out of these tests.
    monkeypatch.setattr(sleep_manager, "_presence_to_rest", lambda note: None)
    monkeypatch.setattr(sleep_manager, "_ensure_presence_sleep", lambda note: None)
    monkeypatch.setattr(sleep_manager, "_record_sleep_wake", lambda started_at, source: None)

    indexer = FakeIndexer()
    monkeypatch.setattr(indexer_module, "get_indexer", lambda *a, **k: indexer)

    mila_db.init_db()
    mila_db.ensure_chat_exists(CHAT_ID, "Chat 1")
    mila_db.ensure_user_exists(USER_ID, "User")
    return SleepEnv(tmp_path, indexer)


@pytest.fixture
def context_delete_failures(monkeypatch):
    """Make DELETE statements on the context buffer raise while ``flags['on']``.

    Simulates the destructive step of a summary (the "pop") failing in the
    middle of a sleep transaction.
    """
    from core.context import buffer

    real_get_conn = buffer._get_conn
    flags = {"on": False}

    class _FailingDeleteConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("DELETE"):
                raise sqlite3.OperationalError("simulated failure during message removal")
            return self._conn.execute(sql, params)

        def commit(self):
            return self._conn.commit()

        def rollback(self):
            return self._conn.rollback()

        def close(self):
            return self._conn.close()

    def factory():
        conn = real_get_conn()
        return _FailingDeleteConnection(conn) if flags["on"] else conn

    monkeypatch.setattr(buffer, "_get_conn", factory)
    return flags

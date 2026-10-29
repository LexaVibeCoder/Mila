"""Smoke tests: config is hermetic and the DB schema initializes in a temp dir."""

import sqlite3

from config import settings


def test_config_uses_dummy_env():
    assert settings.MAIN_LLM_MODEL == "test-model"
    assert settings.LLM_API_BASE == "http://127.0.0.1:9/v1"


def test_init_db_creates_schema(tmp_path, monkeypatch):
    from core import db

    db_path = tmp_path / "mila.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))

    db.init_db()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"users", "chats", "chunks", "memories"} <= tables

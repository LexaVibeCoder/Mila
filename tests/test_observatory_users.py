"""Observatory Users API must not list Mila or empty phantom profiles."""

import pytest
from fastapi import HTTPException

import core.db as db
from Mila_observatory.api import users as users_api


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mila.db"))
    db.init_db()

    # A real peer and a chat with both sides of the dialog.
    db.ensure_chat_exists("chat-1", "Alpha")
    db.ensure_user_exists("user-1", "Alpha")
    db.add_chunk("chat-1", "user-1", "hello")

    # Mila's own outgoing bubbles are stored with her sender id.
    db.ensure_user_exists("bot-1", "Mila")
    db.add_chunk("chat-1", "bot-1", "hi from Mila")
    db.ensure_chat_exists("chat-2", "Beta")
    db.add_chunk("chat-2", "bot-1", "hi in another chat")
    db.set_bot_user_id("bot-1")

    # A row left behind by an old rollback: no data, no manually set name.
    db.ensure_user_exists("ghost-1")
    return tmp_path


@pytest.mark.asyncio
async def test_list_users_hides_mila_and_empty_ghosts(seeded):
    result = await users_api.list_users(search=None, sort="last_activity")

    ids = {item.user_id for item in result.items}
    assert ids == {"user-1"}
    assert result.total == 1


def test_user_detail_rejects_mila(seeded):
    with pytest.raises(HTTPException) as exc:
        users_api._user_detail("bot-1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_users_keeps_named_profile_without_messages(seeded):
    db.ensure_user_exists("user-2", "Beta")

    result = await users_api.list_users(search=None, sort="last_activity")

    ids = {item.user_id for item in result.items}
    assert ids == {"user-1", "user-2"}
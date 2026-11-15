"""Media quarantine during dialog compression (TODO.md S13).

Summarized photos/audio are moved to quarantine, not unlinked. Rollback puts the
bytes and DB references back; commit purges the quarantine.
"""

import os
import time

import pytest

import core.sleep.manager as sm
import core.sleep.processes as sp
from core import db as mila_db
from core.context import buffer
from core.memory import eternal
from core.sleep.state import SleepState, get_state

CHAT = "chat-1"
USER = "user-1"


def _seed_photo(env):
    """One photo (with a .wav twin) referenced by a chat message, plus 11 plain messages."""
    photos = env.media_dir / "photos"
    photos.mkdir(parents=True)
    photo = photos / "alpha.jpg"
    twin = photos / "alpha.wav"
    photo.write_bytes(b"alpha-photo-bytes")
    twin.write_bytes(b"alpha-wav-bytes")

    chunk_id = mila_db.add_chunk(CHAT, USER, "[photo]", kind="photo", media_path=str(photo))
    media_id = mila_db.log_media(CHAT, USER, str(photo), "alpha description")
    env.seed_messages(11)
    buffer.push_message(CHAT, "[photo placeholder]", source_chunk_id=chunk_id)
    return chunk_id, str(photo), str(twin), media_id


def _chunk_media_path(chunk_id):
    return mila_db.get_chunk(chunk_id)["media_path"]


def _media_log_path(env, media_id):
    rows = env._rows("SELECT file_path FROM media_logs WHERE media_id=?", (media_id,))
    return rows[0]["file_path"]


def _snapshot(rows):
    return [(r["id"], r["content"], r["type"]) for r in rows]


async def test_rollback_restores_quarantined_media_and_db_references(sleep_env):
    env = sleep_env
    chunk_id, photo, twin, media_id = _seed_photo(env)
    before = _snapshot(env.messages())
    session = env.begin_sleep()

    await env.stage_summary(session, claimed=12)

    # During the sleep the bytes are out of the original location and refs are detached.
    assert not os.path.exists(photo)
    assert not os.path.exists(twin)
    assert _chunk_media_path(chunk_id) is None
    quarantine = env.media_dir / "quarantine" / session
    assert quarantine.is_dir() and any(quarantine.iterdir())

    await sm.rollback_all(session)

    assert open(photo, "rb").read() == b"alpha-photo-bytes"
    assert open(twin, "rb").read() == b"alpha-wav-bytes"
    assert _chunk_media_path(chunk_id) == photo
    assert _media_log_path(env, media_id) == photo
    assert not quarantine.exists()
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []


async def test_commit_purges_quarantined_media_and_keeps_refs_detached(sleep_env):
    env = sleep_env
    chunk_id, photo, twin, _media_id = _seed_photo(env)
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)

    assert await sm.commit_all(session) is True

    assert not os.path.exists(photo)
    assert not os.path.exists(twin)
    assert not (env.media_dir / "quarantine" / session).exists()
    assert _chunk_media_path(chunk_id) is None
    assert len(env.summaries()) == 1
    assert env.messages() == []


async def test_failed_sleep_restores_media_end_to_end(sleep_env, monkeypatch):
    env = sleep_env
    chunk_id, photo, twin, _media_id = _seed_photo(env)

    async def work(sid):
        await env.stage_summary(sid, claimed=12)

    env.use_processes(monkeypatch, work, ok=False)
    session = env.begin_sleep()

    assert await sm.execute_sleep(session) is False

    assert open(photo, "rb").read() == b"alpha-photo-bytes"
    assert open(twin, "rb").read() == b"alpha-wav-bytes"
    assert _chunk_media_path(chunk_id) == photo


def _chunk_rows(env):
    eternal._ensure_table()
    return env._rows("SELECT chunk_id, title FROM mila_memory_chunks")


# ---------------------------------------------------------------------------
# B1: mixed rollback. The media-bearing summary is staged first; memory, user
# and chunk operations follow. Rollback must undo all of them, restore the
# media bytes and DB refs, and return promptly (no busy_timeout stall).
# ---------------------------------------------------------------------------

async def test_mixed_rollback_restores_media_and_undoes_later_ops_quickly(sleep_env):
    env = sleep_env
    chunk_id, photo, twin, media_id = _seed_photo(env)
    before = _snapshot(env.messages())
    session = env.begin_sleep()

    await env.stage_summary(session, claimed=12)  # media quarantined first
    fact_executor = sp.StagingExecutor(
        session, "user_fact_extraction",
        known_user_ids={"user-2"}, default_source_chat_id=CHAT)
    await fact_executor.execute_tool_call(env.call(
        "add_user_fact", user_id="user-2", content="alpha fact", importance=0.7))
    await sp.StagingExecutor(session, "dialog_summarization").execute_tool_call(env.call(
        "add_memory_chunk", title="alpha title", content_md="alpha body", importance=0.6))

    # Precondition: every kind of op really happened before the rollback.
    assert env.messages() == []
    assert not os.path.exists(photo)
    assert env.memory_count("alpha fact") == 1
    assert mila_db.get_user_info("user-2") is not None
    assert len(_chunk_rows(env)) == 1
    kinds = {r["target_table"] for r in env.staging(session)}
    assert {"mila_memory_chunks", "memories", "users"} <= kinds
    assert any(k.startswith("context_buffer") for k in kinds)

    started = time.monotonic()
    await sm.rollback_all(session)
    elapsed = time.monotonic() - started

    # A busy_timeout stall would take ~30 s; a prompt undo is well under this bound.
    assert elapsed < 5.0, f"rollback took {elapsed:.1f}s (lock contention symptom)"
    # Media bytes and DB refs are back.
    assert open(photo, "rb").read() == b"alpha-photo-bytes"
    assert open(twin, "rb").read() == b"alpha-wav-bytes"
    assert _chunk_media_path(chunk_id) == photo
    assert _media_log_path(env, media_id) == photo
    assert not (env.media_dir / "quarantine" / session).exists()
    # Messages restored, summary gone.
    assert _snapshot(env.messages()) == before
    assert env.summaries() == []
    # Later ops undone.
    assert env.memory_count("alpha fact") == 0
    assert mila_db.get_user_info("user-2") is None
    assert _chunk_rows(env) == []
    assert env.staging(session) == []
    assert get_state() == SleepState.AWAKE


# ---------------------------------------------------------------------------
# M1: a rollback that cannot finish its undo is retryable, not "done".
# ---------------------------------------------------------------------------

async def test_failed_rollback_keeps_staging_and_quarantine_then_retry_completes(
        sleep_env, context_delete_failures):
    env = sleep_env
    chunk_id, photo, twin, media_id = _seed_photo(env)
    before = _snapshot(env.messages())
    session = env.begin_sleep()
    await env.stage_summary(session, claimed=12)
    staged_ids = [r["id"] for r in env.staging(session)]
    assert staged_ids, "the summary must leave undo rows behind"
    quarantine = env.media_dir / "quarantine" / session
    assert quarantine.is_dir()

    context_delete_failures["on"] = True
    await sm.rollback_all(session)
    context_delete_failures["on"] = False

    # The summary delete failed: staging rows stay, quarantine is not purged,
    # and the state records that the night is not cleanly rolled back.
    assert get_state() == SleepState.SLEEP_ERROR
    assert [r["id"] for r in env.staging(session)] == staged_ids
    assert quarantine.exists()

    # Retry with a healthy DB finishes the undo.
    await sm.rollback_all(session)

    assert get_state() == SleepState.AWAKE
    assert env.staging(session) == []
    assert not quarantine.exists()
    assert env.summaries() == []
    assert _snapshot(env.messages()) == before
    assert open(photo, "rb").read() == b"alpha-photo-bytes"
    assert open(twin, "rb").read() == b"alpha-wav-bytes"
    assert _chunk_media_path(chunk_id) == photo
    assert _media_log_path(env, media_id) == photo

"""Observatory test chat — sandboxed dialogs with Mila.

Each session runs the real pipeline in the session's configured state with
``sandbox=True``: Mila reads the synthetic interlocutor's memories and writes
the session's own diary, while `chunks`, real context, global presence, the
real diary and long-term memory stay untouched. Everything lives in
``test_chat_*`` tables and is deleted with the session.
"""

import sys
import os
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import APIRouter, HTTPException

from Mila_observatory.api.dashboard import add_event
from Mila_observatory.llm_manager import llm_manager
from Mila_observatory.models import (
    TestChatDiaryEntry,
    TestChatDiaryRequest,
    TestChatDiaryUpdateRequest,
    TestChatMessage,
    TestChatMeta,
    TestChatSendRequest,
    TestChatSendResponse,
    TestChatSession,
    TestChatSessionCreateRequest,
    TestChatSessionDetail,
    TestChatSessionUpdateRequest,
)
from core.test_chat import store
from core.test_chat.runner import run_turn

router = APIRouter(tags=["test-chat"])


def _session_or_404(session_id: str) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Test session not found")
    return session


@router.get("/api/test-chat/meta", response_model=TestChatMeta)
async def test_chat_meta():
    """Defaults and capabilities of the sandboxed test dialog."""
    model = ""
    try:
        model = llm_manager.get_main_params().get("model", "") or ""
    except Exception:
        pass
    return TestChatMeta(
        default_user_name=store.DEFAULT_USER_NAME,
        default_state=store.DEFAULT_STATE,
        states=list(store.VALID_STATES),
        tools=["search_memory", "write_diary", "update_diary",
               "send_message", "enter_dialog", "enter_rest",
               "enter_working", "start_sleep"],
        model=model,
    )


@router.get("/api/test-chat/sessions", response_model=List[TestChatSession])
async def list_test_sessions():
    """All test dialogs, newest first."""
    return [TestChatSession(**row) for row in store.list_sessions()]


@router.post("/api/test-chat/sessions", response_model=TestChatSession)
async def create_test_session(req: TestChatSessionCreateRequest | None = None):
    """Create an empty test dialog (title, interlocutor name, state)."""
    session = store.create_session(
        title=req.title if req else None,
        user_name=req.user_name if req else None,
        state=req.state if req else None,
    )
    add_event("test_chat_create", f"Test chat '{session['title']}' created")
    return TestChatSession(**session)


@router.get("/api/test-chat/sessions/{session_id}", response_model=TestChatSessionDetail)
async def get_test_session(session_id: str):
    """Session metadata plus its whole message history."""
    session = _session_or_404(session_id)
    messages = [TestChatMessage(**row) for row in store.get_messages(session_id)]
    return TestChatSessionDetail(session=TestChatSession(**session),
                                 messages=messages)


@router.put("/api/test-chat/sessions/{session_id}", response_model=TestChatSession)
async def update_test_session(session_id: str, req: TestChatSessionUpdateRequest):
    """Rename the scenario/interlocutor or change Mila's session state."""
    session = store.update_session(
        session_id,
        title=req.title,
        user_name=req.user_name,
        state=req.state,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Test session not found")
    add_event("test_chat_update", f"Test chat {session_id[:8]}... updated")
    return TestChatSession(**session)


@router.delete("/api/test-chat/sessions/{session_id}")
async def delete_test_session(session_id: str):
    """Delete the dialog and every row it owns — Mila no longer remembers it."""
    if not store.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Test session not found")
    add_event("test_chat_delete", f"Test chat {session_id[:8]}... deleted")
    return {"success": True, "message": "Test session deleted"}


@router.delete("/api/test-chat/sessions/{session_id}/messages")
async def clear_test_session(session_id: str):
    """Keep the session (and its diary), wipe messages and debug data."""
    _session_or_404(session_id)
    deleted = store.clear_messages(session_id)
    add_event("test_chat_clear",
              f"Test chat {session_id[:8]}... cleared ({deleted} messages)")
    return {"success": True, "deleted": deleted}


@router.post("/api/test-chat/sessions/{session_id}/messages",
             response_model=TestChatSendResponse)
async def send_test_message(session_id: str, req: TestChatSendRequest):
    """Run one sandboxed pipeline turn and return the new messages."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    try:
        data = await run_turn(session_id, req.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Test session not found")
    add_event("test_chat_message", f"Test chat {session_id[:8]}...: turn done")
    return TestChatSendResponse(
        session_id=session_id,
        session=TestChatSession(**data["session"]) if data.get("session") else None,
        messages=[TestChatMessage(**row) for row in data["messages"]],
        debug=data.get("debug"),
    )


# ── session diary ────────────────────────────────────────────────

@router.get("/api/test-chat/sessions/{session_id}/diary",
            response_model=List[TestChatDiaryEntry])
async def list_test_diary(session_id: str):
    """Entries of the session's own diary (never the real one)."""
    _session_or_404(session_id)
    return [TestChatDiaryEntry(**row) for row in store.get_diary_entries(session_id)]


@router.post("/api/test-chat/sessions/{session_id}/diary",
             response_model=TestChatDiaryEntry)
async def create_test_diary_entry(session_id: str, req: TestChatDiaryRequest):
    """Add an entry by hand (the UI panel also uses this)."""
    _session_or_404(session_id)
    entry = store.add_diary_entry(session_id, req.content, req.category)
    if not entry:
        raise HTTPException(status_code=400, detail="Empty diary entry")
    return TestChatDiaryEntry(**entry)


@router.put("/api/test-chat/sessions/{session_id}/diary/{entry_id}",
            response_model=TestChatDiaryEntry)
async def update_test_diary_entry(session_id: str, entry_id: int,
                                  req: TestChatDiaryUpdateRequest):
    """Edit a session diary entry (also used by Mila's update_diary)."""
    _session_or_404(session_id)
    updated = store.update_diary_entry(
        session_id, entry_id, req.content, category=req.category)
    if not updated:
        raise HTTPException(status_code=404, detail="Diary entry not found")
    entries = store.get_diary_entries(session_id)
    for entry in entries:
        if entry["id"] == entry_id:
            return TestChatDiaryEntry(**entry)
    raise HTTPException(status_code=404, detail="Diary entry not found")


@router.delete("/api/test-chat/sessions/{session_id}/diary/{entry_id}")
async def delete_test_diary_entry(session_id: str, entry_id: int):
    """Delete one entry from the session diary."""
    _session_or_404(session_id)
    if not store.delete_diary_entry(session_id, entry_id):
        raise HTTPException(status_code=404, detail="Diary entry not found")
    return {"success": True}

"""Run one sandboxed Mila turn for the Observatory test chat.

The real pipeline is used (same persona, prompt assembly, tool loop and LLM
client as the running bot). The turn is pinned to the session's own state and
``sandbox=True``: no global DB row is written, while the session diary,
session state and the single sandbox chat are fully functional.
"""

import asyncio
import uuid
from datetime import datetime

from core.db import get_bot_user_id
from core.delivery import TurnContext
from core.memory.indexer import get_indexer
from core.pipeline import ThinkPipeline
from core.test_chat import store
from core.test_chat.context import SandboxContext

# A single turn can involve several LLM calls plus reranking; keep the HTTP
# request bounded instead of hanging forever.
TURN_TIMEOUT_SECONDS = 180

_locks: dict[str, asyncio.Lock] = {}


def _lock(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _presence(session: dict) -> dict:
    """Turn-local presence snapshot; global presence is never touched."""
    state = store.normalize_state(session.get("state"))
    in_dialog = state == "dialog"
    return {
        "state": state,
        "focus_chat_id": session["chat_id"] if in_dialog else None,
        "focus_user_name": session["user_name"] if in_dialog else None,
        "focus_chat_alias": session["alias"] if in_dialog else None,
        # Alias of the notification chat (header + rest/working notification).
        "chat_alias": session["alias"],
        "user_name": session["user_name"],
        "note": "",
        "updated_at": datetime.now().isoformat(),
    }


def _debug_payload(result: dict) -> dict:
    return {
        "system_prompt": result.get("system_prompt") or "",
        "iterations": result.get("raw_llm_responses") or [],
        "actions": result.get("actions") or [],
        "timing": result.get("timing") or {},
        "reply_via": result.get("reply_via") or "",
        "silence_reason": result.get("silence_reason") or "",
        "error": result.get("error"),
    }


async def run_turn(session_id: str, text: str) -> dict:
    """Append a user message, run one pipeline turn, store the reply.

    Returns ``{"messages": [user, assistant...], "debug": {...},
    "session": {...}}``. The user message is written before generation so the
    model's own history matches a real dialog; if the turn fails an ``error``
    row is stored instead of a reply.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")

    session = store.get_session(session_id)
    if not session:
        raise KeyError(session_id)

    async with _lock(session_id):
        # Re-read inside the lock: another turn may have deleted the session.
        session = store.get_session(session_id)
        if not session:
            raise KeyError(session_id)

        user_row = store.append_message(session_id, "user", text, kind="text")
        history = store.build_history(session_id)
        sandbox_context = SandboxContext(
            session_id=session_id,
            chat_id=session["chat_id"],
            alias=session["alias"],
            user_name=session["user_name"],
            user_id=session["user_id"],
            state=session["state"],
        )

        try:
            indexer = get_indexer()
            pipeline = ThinkPipeline(
                text_memory=indexer.vector,
                embedding_client=indexer.embedder,
                user_id=session["user_id"],
                chat_id=session["chat_id"],
                my_user_id=get_bot_user_id(),
            )
            presence = _presence(session)
            turn = TurnContext(
                turn_id=uuid.uuid4().hex,
                chat_id=session["chat_id"],
                user_id=session["user_id"],
                presence=presence,
            )
            result = await asyncio.wait_for(
                pipeline.process_message(
                    user_message=text,
                    user_visible_history=history,
                    agent_internal_history=history,
                    request_user_id=session["user_id"],
                    request_chat_id=session["chat_id"],
                    turn=turn,
                    sandbox=True,
                    sandbox_context=sandbox_context,
                    sandbox_diary_context=store.get_diary_context(session_id),
                ),
                timeout=TURN_TIMEOUT_SECONDS,
            )
        except Exception as e:
            debug = {"error": f"{type(e).__name__}: {e}"}
            row = store.append_message(
                session_id, "assistant", "", kind="error", debug=debug)
            return {
                "messages": [user_row, row],
                "debug": debug,
                "session": store.get_session(session_id),
            }

        debug = _debug_payload(result)
        texts = [t for t in (result.get("reply_texts") or [])
                 if (t or "").strip()]

        if result.get("success") and texts:
            messages = [user_row]
            for index, bubble in enumerate(texts):
                messages.append(store.append_message(
                    session_id, "assistant", bubble, kind="bubble",
                    debug=debug if index == 0 else None,
                ))
            return {
                "messages": messages,
                "debug": debug,
                "session": store.get_session(session_id),
            }

        if result.get("success"):
            reason = result.get("silence_reason") or "empty reply"
            row = store.append_message(
                session_id, "assistant", "", kind="silence",
                debug={**debug, "silence_reason": reason},
            )
            return {
                "messages": [user_row, row],
                "debug": debug,
                "session": store.get_session(session_id),
            }

        error = result.get("error") or "pipeline error"
        row = store.append_message(
            session_id, "assistant", "", kind="error",
            debug={**debug, "error": error},
        )
        return {
            "messages": [user_row, row],
            "debug": debug,
            "session": store.get_session(session_id),
        }

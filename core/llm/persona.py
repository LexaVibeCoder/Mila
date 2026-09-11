"""System prompt assembly for Mila.

Prompt text lives in ``core/llm/prompts/`` grouped by folders:
- persona/      — who Mila is, always included (static, cacheable)
- relationship/ — tier line for the current user (dynamic)
- context/      — header, needs, diary block, session, history note
- diary/        — diary guide (static)
- states/       — behavior for the CURRENT presence state only
  (rest.md / dialog.md / working.md — exactly one is included)

Rule: the model sees only what she needs right now. Rest never sees
dialog JSON rules; dialog never sees rest transition rules beyond
the foreign-notification case.
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from core.db import get_user_info, get_recent_chunks
from core.diary.manager import get_today_diary_context
from core.needs.tracker import get_tracker as get_needs_tracker

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(name: str) -> str:
    """Load a prompt fragment by relative path without extension.

    e.g. _load("persona/voice") -> prompts/persona/voice.md.
    Falls back to the old flat layout for backward compat.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        flat = PROMPTS_DIR / f"{name.upper()}.md"
        if flat.is_file():
            path = flat
        else:
            raise FileNotFoundError(f"Prompt fragment not found: {name}")
    return path.read_text(encoding="utf-8").strip()


# Static persona blocks — always included, order matters (stable first).
_STATIC_PERSONA = [
    "persona/prefs",
    "persona/bio",
    "persona/identity",
    "persona/closeness",
    "persona/voice",
    "persona/banned",
    "persona/memory",
    "persona/style",
    "persona/no_roleplay",
    "diary/guide",
]

# Mapping of old flat names -> new paths (for custom_persona checks etc.)
LEGACY_MAP = {
    "MILA_PREFERENCES": "persona/prefs",
    "MILA_BIO": "persona/bio",
    "IDENTITY": "persona/identity",
    "CLOSENESS_TIERS": "persona/closeness",
    "VOICE_MECHANICS": "persona/voice",
    "BANNED_AI_ISMS": "persona/banned",
    "MEMORY_HONESTY": "persona/memory",
    "STYLE_STABILITY": "persona/style",
    "NO_ROLEPLAY": "persona/no_roleplay",
    "DIARY_GUIDE": "diary/guide",
    "SILENT_MODE": "context/history_note",
    "HEADER": "context/header",
    "NEEDS_BLOCK": "context/needs",
    "DIARY_BLOCK": "context/diary_block",
    "SESSION_BLOCK": "context/session",
    "SESSION_TIME": "context/session_time",
    "RELATIONSHIP_BLOCK": "relationship/block",
    "RELATIONSHIP_FRIEND": "relationship/friend",
    "RELATIONSHIP_ACQUAINTANCE_WARM": "relationship/acq_warm",
    "RELATIONSHIP_ACQUAINTANCE_COLD": "relationship/acq_cold",
    "RELATIONSHIP_STRANGER_COLD": "relationship/stranger",
    "RELATIONSHIP_NEUTRAL": "relationship/neutral",
}

REL_MAP = {
    "RELATIONSHIP_FRIEND": "relationship/friend",
    "RELATIONSHIP_ACQUAINTANCE_WARM": "relationship/acq_warm",
    "RELATIONSHIP_ACQUAINTANCE_COLD": "relationship/acq_cold",
    "RELATIONSHIP_STRANGER_COLD": "relationship/stranger",
    "RELATIONSHIP_NEUTRAL": "relationship/neutral",
}


def load_prompt(name: str) -> str:
    """Public loader with legacy-name support."""
    return _load(LEGACY_MAP.get(name, name))


def _format_time_delta(delta: timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    days = total_minutes // (60 * 24)
    hours = (total_minutes % (60 * 24)) // 60
    minutes = total_minutes % 60
    if days > 0:
        return f"{days} days {hours} hours {minutes} minutes"
    elif hours > 0:
        return f"{hours} hours {minutes} minutes"
    else:
        return f"{minutes} minutes"


def _get_profile_context(user_id: str) -> tuple[str, str, str]:
    user = get_user_info(user_id)
    if not user:
        user_name = "user"
        sentiment = 0.0
    else:
        user_name = user.get("preferred_name") or user.get("user_name") or "user"
        sentiment = user.get("sentiment", 0.0)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # sentiment maps to closeness tier: друг / знакомый / чужой.
    # history length refines it in the prompt (just met -> чужой even if sentiment neutral).
    if sentiment > 0.6:
        template = "relationship/friend"
    elif sentiment > 0.2:
        template = "relationship/acq_warm"
    elif sentiment < -0.6:
        template = "relationship/stranger"
    elif sentiment < -0.2:
        template = "relationship/acq_cold"
    else:
        template = "relationship/neutral"
    relationship = _load(template).replace("{user_name}", user_name)
    return user_name, relationship, now


def _get_session_time_info(chat_id: str, my_user_id: str) -> str:
    chunks = get_recent_chunks(chat_id, limit=50)
    now_dt = datetime.now(timezone.utc)
    for chunk in reversed(chunks):
        if chunk.get("user_id") == my_user_id and chunk.get("time_start"):
            last_ai_time_raw = chunk["time_start"]
            try:
                if isinstance(last_ai_time_raw, str):
                    if 'T' in last_ai_time_raw:
                        last_ai_dt = datetime.fromisoformat(last_ai_time_raw)
                    else:
                        last_ai_dt = datetime.strptime(last_ai_time_raw, '%Y-%m-%d %H:%M:%S')
                else:
                    last_ai_dt = last_ai_time_raw
                if isinstance(last_ai_dt, datetime) and last_ai_dt.tzinfo is None:
                    last_ai_dt = last_ai_dt.replace(tzinfo=timezone.utc)
                delta = now_dt - last_ai_dt
                total_seconds = delta.total_seconds()
                if total_seconds < 30:
                    return ""
                formatted = _format_time_delta(delta)
                return _load("context/session_time").replace("{formatted}", formatted)
            except (ValueError, TypeError):
                continue
    return ""


def _get_presence_state_name() -> str:
    try:
        from core.presence import get_presence
        return get_presence().get("state", "rest")
    except Exception:
        return "rest"


@lru_cache(maxsize=1)
def _get_static_prompt_prefix() -> str:
    return "\n\n---\n\n".join(_load(name) for name in _STATIC_PERSONA)


def _load_state_block(state: str, chat_id: str | None = None) -> str:
    """Exactly one state file. Unknown states fall back to rest."""
    name = state if state in ("rest", "dialog", "working") else "rest"
    text = _load(f"states/{name}")
    return text.replace("{chat_id}", str(chat_id or ""))


def build_main_llm_system_prompt(user_id: str, chat_id: str = None, my_user_id: str = None) -> str:
    user_name, relationship, now = _get_profile_context(user_id)
    static_prefix = _get_static_prompt_prefix()

    try:
        from Mila_observatory.runtime import runtime_config
        custom_persona = runtime_config.get("persona.custom_text", "")
        if custom_persona:
            static_prefix = custom_persona
    except ImportError:
        pass

    needs_summary = ""
    try:
        needs_summary = get_needs_tracker().get_needs_summary()
    except Exception:
        pass

    header = (
        _load("context/header")
        .replace("{now}", now)
        .replace("{user_name}", user_name)
        .replace("{chat_id}", str(chat_id))
    )
    relationship_block = _load("relationship/block").replace("{relationship}", relationship)
    needs_block = _load("context/needs").replace("{needs_summary}", needs_summary)
    history_note = _load("context/history_note")
    try:
        media_note = _load("context/media_note")
    except Exception:
        media_note = ""

    parts = [header, static_prefix, relationship_block, needs_block, history_note]
    if media_note:
        parts.append(media_note)

    # Presence block — location only (where am I + who writes).
    # Behavior rules live in states/{state}.md, not here.
    try:
        from core.presence import format_state_block
        parts.append(format_state_block(
            current_chat_id=str(chat_id) if chat_id else None,
            current_user_name=user_name,
        ))
    except Exception:
        pass

    diary_context = get_today_diary_context()
    if diary_context:
        parts.append(_load("context/diary_block").replace("{diary_context}", diary_context))

    if chat_id and my_user_id:
        session_time = _get_session_time_info(chat_id, my_user_id)
        if session_time:
            parts.append(_load("context/session").replace("{session_time}", session_time))

    # State behavior — exactly one file, only what is needed now.
    state = _get_presence_state_name()
    parts.append(_load_state_block(state, chat_id))

    return "\n\n".join(parts).strip()

"""System prompt assembly for Mila.

No prompt/instruction text lives here. All fragments are stored as
Markdown files in ``core/llm/prompts/`` and this module only loads
them and assembles the final system prompt with runtime context
(user profile, needs, diary, session state).

Static blocks (joined with ``---``), in order:

- MILA_PREFERENCES — base voice
- MILA_BIO — bio section (includes its header)
- IDENTITY — stable core identity
- CLOSENESS_TIERS — distance tiers + examples
- VOICE_MECHANICS — voice mechanics
- BANNED_AI_ISMS — banned AI-isms
- MEMORY_HONESTY — memory honesty
- SILENT_MODE — silent mode in history
- STYLE_STABILITY — style stability rule
- NO_ROLEPLAY — no role-play / acting
- DIARY_GUIDE — diary guide (static instructions)

Dynamic templates (``{placeholders}`` filled at runtime):

- HEADER — intro with ``{now}``, ``{user_name}``, ``{chat_id}``
- RELATIONSHIP_BLOCK — wrapper with ``{relationship}``
- RELATIONSHIP_{FRIEND,ACQUAINTANCE_WARM,STRANGER_COLD,ACQUAINTANCE_COLD,NEUTRAL}
  — relationship line variants with ``{user_name}``, picked by sentiment
- NEEDS_BLOCK — wrapper with ``{needs_summary}``
- DIARY_BLOCK — wrapper with ``{diary_context}``
- SESSION_BLOCK — wrapper with ``{session_time}``
- SESSION_TIME — ``User responded after {formatted}``
- FUNCTION_CALLING — ``think`` protocol with ``{chat_id}``
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from core.db import get_user_info, get_recent_chunks
from core.diary.manager import get_today_diary_context
from core.needs.tracker import get_tracker as get_needs_tracker

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_STATIC_PARTS = [
    "MILA_PREFERENCES",
    "MILA_BIO",
    "IDENTITY",
    "CLOSENESS_TIERS",
    "VOICE_MECHANICS",
    "BANNED_AI_ISMS",
    "MEMORY_HONESTY",
    "SILENT_MODE",
    "STYLE_STABILITY",
    "NO_ROLEPLAY",
    "DIARY_GUIDE",
]


@lru_cache(maxsize=None)
def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt fragment not found: {path}")
    return path.read_text(encoding="utf-8").strip()


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
        template = "RELATIONSHIP_FRIEND"
    elif sentiment > 0.2:
        template = "RELATIONSHIP_ACQUAINTANCE_WARM"
    elif sentiment < -0.6:
        template = "RELATIONSHIP_STRANGER_COLD"
    elif sentiment < -0.2:
        template = "RELATIONSHIP_ACQUAINTANCE_COLD"
    else:
        template = "RELATIONSHIP_NEUTRAL"
    relationship = _load_prompt(template).replace("{user_name}", user_name)
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
                return _load_prompt("SESSION_TIME").replace("{formatted}", formatted)
            except (ValueError, TypeError):
                continue
    return ""


@lru_cache(maxsize=1)
def _get_static_prompt_prefix() -> str:
    return "\n\n---\n\n".join(_load_prompt(name) for name in _STATIC_PARTS)


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
        _load_prompt("HEADER")
        .replace("{now}", now)
        .replace("{user_name}", user_name)
        .replace("{chat_id}", str(chat_id))
    )
    relationship_block = _load_prompt("RELATIONSHIP_BLOCK").replace("{relationship}", relationship)
    needs_block = _load_prompt("NEEDS_BLOCK").replace("{needs_summary}", needs_summary)

    parts = [header, static_prefix, relationship_block, needs_block]

    # Presence block — Mila explicitly sees her state + who writes (name, not id).
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
        parts.append(_load_prompt("DIARY_BLOCK").replace("{diary_context}", diary_context))

    if chat_id and my_user_id:
        session_time = _get_session_time_info(chat_id, my_user_id)
        if session_time:
            parts.append(_load_prompt("SESSION_BLOCK").replace("{session_time}", session_time))

    parts.append(_load_prompt("FUNCTION_CALLING").replace("{chat_id}", str(chat_id)))

    return "\n\n".join(parts).strip()

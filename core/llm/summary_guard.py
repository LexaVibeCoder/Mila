"""core/llm/summary_guard.py — language contract for LLM-generated summaries.

Both summarizing paths (night reflection in ``core/sleep/processes.py`` and the
daytime context compressor in ``core/bot.py``) share the same rules: Russian
only, no translated proper names, and an explicit glossary for ambiguous
English concepts (TODO.md S10/S11). The CJK detector backs the post-generation
guard when a model drifts into Chinese. The helpers at the bottom add the
stored period prefix so both paths write the same summary format.
"""

import re

# Injected into every summarization prompt verbatim.
SUMMARY_LANGUAGE_RULES = """Language rules for the summary:
- Write the summary in Russian only. Do not use Chinese, English or any other
  language words — Russian text with names/handles is the only allowed form.
- Glossary for ambiguous English concepts (translate the meaning, not the letters):
  - "promotion" → «продвижение» / «представление» (never «сброс», never "reset")
- Do not translate proper names, usernames, handles, channel names or product names."""

# Injected into every summarization prompt verbatim (daytime and night paths).
SUMMARY_STYLE_RULES = """Style rules for the summary:
- Write the summary as Mila in the first person, e.g. «Я обсуждала с user-1 тему X и мы договорились о Y».
- Keep it neutral and factual: no colloquialisms, no persona slang, no evaluations or emotional embellishment.
- Do not add a date or period prefix (like «[2026-01-01]») yourself — the code prepends it."""

# CJK ideographs, kana, hangul and compatibility ideographs.
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A summary that already carries a "[YYYY-MM-DD ...]" prefix is left untouched.
_PERIOD_PREFIX_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}")


def contains_cjk(text: str) -> bool:
    """True when ``text`` contains CJK/Kana/Hangul characters (S10)."""
    return bool(text) and _CJK_RE.search(str(text)) is not None


def _local_date(raw) -> str:
    """Local ``YYYY-MM-DD`` for a stored UTC timestamp, or '' when unparseable.

    Imported lazily: ``core.context.buffer`` imports ``config`` and the token
    counter, so a module-level import here would make the dependency circular.
    """
    if not raw:
        return ""
    try:
        from core.context.buffer import format_created_at_local

        text = str(format_created_at_local(raw) or "")[:10]
    except Exception:
        return ""
    return text if _ISO_DATE_RE.match(text) else ""


def period_prefix_from_messages(messages) -> str:
    """``[YYYY-MM-DD — YYYY-MM-DD] `` for the local date span of ``messages``.

    Returns '' when no message carries a parseable date, so the caller can leave
    the text unchanged. A single day collapses to ``[YYYY-MM-DD] ``.
    """
    dates = sorted({
        date for date in (_local_date((m or {}).get("created_at")) for m in messages or [])
        if date
    })
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if first == last:
        return f"[{first}] "
    return f"[{first} — {last}] "


def add_period_prefix(summary_text: str, messages) -> str:
    """Prepend the local date span unless ``summary_text`` already has one."""
    text = str(summary_text or "").strip()
    if not text or _PERIOD_PREFIX_RE.match(text):
        return text
    prefix = period_prefix_from_messages(messages)
    return f"{prefix}{text}" if prefix else text

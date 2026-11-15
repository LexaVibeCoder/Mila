"""core/llm/summary_guard.py — language contract for LLM-generated summaries.

Both summarizing paths (night reflection in ``core/sleep/processes.py`` and the
daytime context compressor in ``core/bot.py``) share the same rules: Russian
only, no translated proper names, and an explicit glossary for ambiguous
English concepts (TODO.md S10/S11). The CJK detector backs the post-generation
guard when a model drifts into Chinese.
"""

import re

# Injected into every summarization prompt verbatim.
SUMMARY_LANGUAGE_RULES = """Language rules for the summary:
- Write the summary in Russian only. Do not use Chinese, English or any other
  language words — Russian text with names/handles is the only allowed form.
- Glossary for ambiguous English concepts (translate the meaning, not the letters):
  - "promotion" → «продвижение» / «представление» (never «сброс», never "reset")
- Do not translate proper names, usernames, handles, channel names or product names."""

# CJK ideographs, kana, hangul and compatibility ideographs.
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def contains_cjk(text: str) -> bool:
    """True when ``text`` contains CJK/Kana/Hangul characters (S10)."""
    return bool(text) and _CJK_RE.search(str(text)) is not None

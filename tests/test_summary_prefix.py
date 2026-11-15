"""Period prefix + first-person style rules for dialog summaries (spec 1-4).

Stored summaries carry a code-added local period prefix:
``[YYYY-MM-DD] <text>`` for one local day, ``[YYYY-MM-DD — YYYY-MM-DD] <text>``
(em dash U+2014, min→max) for several. The LLM must not write it; an already
prefixed text is not prefixed again; unparseable dates leave the text alone.
Both summarizing paths share ``core.llm.summary_guard`` for the prefix and for
the first-person, neutral style rules.

Hermetic like the other sleep tests: temp SQLite, synthetic data only
(user-1, chat-1, "alpha fact"/"alpha summary"), no LLM/network/Telegram.
"""

import re
from datetime import timedelta, timezone

import pytest

import core.sleep.processes as sp
from config import settings
from core import db as mila_db
from core.context import buffer
from core.llm.summary_guard import (
    SUMMARY_LANGUAGE_RULES,
    SUMMARY_STYLE_RULES,
    add_period_prefix,
    period_prefix_from_messages,
)

CHAT = "chat-1"
USER = "user-1"
EM_DASH = "\u2014"

_PREFIX_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}(?: \u2014 \d{4}-\d{2}-\d{2})?\] ")


@pytest.fixture
def utc_local(monkeypatch):
    """Pin the configured local zone to UTC so stored UTC dates are local dates."""
    monkeypatch.setattr(settings, "LOCAL_TIMEZONE", "UTC")
    return "UTC"


def _set_created_at(rows, stamps):
    """Rewrite created_at of seeded buffer rows (synthetic dates only).

    ``stamps`` is a single stamp applied to every row, or a list parallel to
    ``rows``; ``None`` entries are left untouched.
    """
    conn = buffer._get_conn()
    try:
        table = buffer._table_name(CHAT)
        for row, stamp in zip(rows, stamps if isinstance(stamps, list) else [stamps] * len(rows)):
            if stamp is None:
                continue
            conn.execute(
                f"UPDATE [{table}] SET created_at=? WHERE id=?", (stamp, row["id"]))
        conn.commit()
    finally:
        conn.close()
    return buffer.get_context(CHAT, limit=100000)


def _reseed(env, stamps):
    rows = env.seed_messages(12)
    return _set_created_at(rows, stamps)


def _seed_chunks(n=10):
    for i in range(n):
        mila_db.add_chunk(CHAT, USER, f"alpha chunk {i}")


class ScriptedLLM:
    """Returns scripted responses in order; the last one repeats."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def generate(self, messages, system_prompt=None, tools=None,
                       tool_choice=None, max_tokens=None, temperature=None):  # noqa: ARG002
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "system_prompt": system_prompt or "",
        })
        index = min(len(self.calls), len(self._responses)) - 1
        return self._responses[index]


def _summary_response(env, text, claimed=12):
    return {"tool_calls": [env.call(
        "save_dialog_summary", chat_id=CHAT, summary_text=text, message_count=claimed)]}


def _approve_response(env):
    return {"tool_calls": [env.call("approve_changes", notes="looks fine")]}


# ---------------------------------------------------------------------------
# Helper (spec 2): period_prefix_from_messages / add_period_prefix
# ---------------------------------------------------------------------------

def test_helper_single_day_prefix(utc_local):
    msgs = [{"created_at": "2026-01-02 08:00:00"},
            {"created_at": "2026-01-02 22:30:00"}]
    assert period_prefix_from_messages(msgs) == "[2026-01-02] "
    assert add_period_prefix("alpha summary", msgs) == "[2026-01-02] alpha summary"


def test_helper_multi_day_prefix_is_min_to_max_with_em_dash(utc_local):
    # Deliberately unordered input: the span is min→max, not first→last seen.
    msgs = [{"created_at": "2026-01-03 12:00:00"},
            {"created_at": "2026-01-01 12:00:00"},
            {"created_at": "2026-01-02 12:00:00"}]
    assert period_prefix_from_messages(msgs) == f"[2026-01-01 {EM_DASH} 2026-01-03] "
    assert "\u2014" in period_prefix_from_messages(msgs)
    # A plain hyphen must not be used as the range separator.
    assert "2026-01-01 - 2026-01-03" not in period_prefix_from_messages(msgs)


def test_helper_uses_local_date_not_utc_date(utc_local, monkeypatch):
    # 2026-01-01 20:00 UTC is already 2026-01-02 at a fixed +09:00 local offset.
    monkeypatch.setattr(buffer, "_local_zone", lambda: timezone(timedelta(hours=9)))
    msgs = [{"created_at": "2026-01-01 20:00:00"}]
    assert period_prefix_from_messages(msgs) == "[2026-01-02] "
    assert add_period_prefix("alpha summary", msgs) == "[2026-01-02] alpha summary"


@pytest.mark.parametrize("msgs", [
    [],
    None,
    [{"created_at": ""}],
    [{"created_at": None}],
    [{"created_at": "not-a-date"}],
    [{}],
])
def test_helper_missing_or_unparseable_dates_leave_text_unchanged(utc_local, msgs):
    assert period_prefix_from_messages(msgs) == ""
    assert add_period_prefix("alpha summary", msgs) == "alpha summary"


def test_helper_already_prefixed_text_is_not_duplicated(utc_local):
    msgs = [{"created_at": "2026-01-03 12:00:00"}]
    assert add_period_prefix("[2026-01-01] alpha summary", msgs) == (
        "[2026-01-01] alpha summary")
    ranged = f"[2026-01-01 {EM_DASH} 2026-01-03] alpha summary"
    assert add_period_prefix(ranged, msgs) == ranged
    # Surrounding whitespace is trimmed, the prefix itself is left alone.
    assert add_period_prefix("  [2026-01-01] alpha summary  ", msgs) == (
        "[2026-01-01] alpha summary")


def test_helper_empty_text_does_not_crash(utc_local):
    msgs = [{"created_at": "2026-01-01 12:00:00"}]
    assert add_period_prefix("", msgs) == ""
    assert add_period_prefix("   ", msgs) == ""


# ---------------------------------------------------------------------------
# Stored prefix via the staging executor (spec 2, night path)
# ---------------------------------------------------------------------------

async def test_staged_single_day_summary_gets_single_day_prefix(sleep_env, utc_local):
    env = sleep_env
    _reseed(env, "2026-01-02 12:00:00")
    session = env.begin_sleep()

    result = await env.stage_summary(session, text="alpha summary", claimed=12)

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries()] == ["[2026-01-02] alpha summary"]


async def test_staged_multi_day_summary_gets_min_max_prefix(sleep_env, utc_local):
    env = sleep_env
    _reseed(env, ["2026-01-01 12:00:00"] * 6 + ["2026-01-03 12:00:00"] * 6)
    session = env.begin_sleep()

    result = await env.stage_summary(session, text="alpha summary", claimed=12)

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries()] == [
        f"[2026-01-01 {EM_DASH} 2026-01-03] alpha summary"]
    log = env.summary_log(session)
    assert [r["summary_text"] for r in log] == [
        f"[2026-01-01 {EM_DASH} 2026-01-03] alpha summary"]
    assert [r["messages_removed"] for r in log] == [12]


async def test_staged_summary_without_parseable_dates_is_stored_unchanged(
        sleep_env, utc_local):
    env = sleep_env
    _reseed(env, "")
    session = env.begin_sleep()

    result = await env.stage_summary(session, text="alpha summary", claimed=12)

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries()] == ["alpha summary"]


async def test_staged_already_prefixed_summary_is_not_duplicated(sleep_env, utc_local):
    env = sleep_env
    _reseed(env, "2026-01-03 12:00:00")
    session = env.begin_sleep()
    already = "[2026-01-01] alpha summary"

    result = await env.stage_summary(session, text=already, claimed=12)

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries()] == [already]


async def test_night_process_stores_prefixed_summary_and_preserves_body(
        sleep_env, utc_local, monkeypatch):
    """process_dialog_summarization: LLM body has no prefix; stored text does,
    and the body survives verbatim after it (spec 2, night path)."""
    env = sleep_env
    _seed_chunks(10)
    _reseed(env, "2026-01-02 12:00:00")
    session = env.begin_sleep()
    plain = "Я обсудила с user-1 тему alpha и мы договорились о Y."
    night = ScriptedLLM([_summary_response(env, plain)])
    verifier = ScriptedLLM([_approve_response(env)])
    monkeypatch.setattr(sp, "_create_night_llm", lambda: night)
    monkeypatch.setattr(sp, "_create_verifier_llm", lambda: verifier)

    ok, _message = await sp.process_dialog_summarization(session)

    assert ok is True
    stored = [s["content"] for s in env.summaries()]
    assert stored == [f"[2026-01-02] {plain}"]
    assert stored[0].endswith(plain)


# ---------------------------------------------------------------------------
# First-person, neutral style (spec 1) + preserved guards (spec 3, 4)
# ---------------------------------------------------------------------------

def test_shared_style_rules_require_first_person_neutral():
    assert "Я" in SUMMARY_STYLE_RULES
    lowered = SUMMARY_STYLE_RULES.lower()
    assert "first person" in lowered
    assert "neutral" in lowered
    assert "factual" in lowered
    # The LLM is told not to write the prefix itself; the code prepends it.
    assert "prefix" in lowered
    assert "Do not add a date" in SUMMARY_STYLE_RULES


async def test_night_prompt_includes_style_and_language_rules(
        sleep_env, utc_local, monkeypatch):
    env = sleep_env
    _seed_chunks(10)
    env.seed_messages(12)
    session = env.begin_sleep()
    night = ScriptedLLM([_summary_response(env, "Я обсудила тему alpha.")])
    verifier = ScriptedLLM([_approve_response(env)])
    monkeypatch.setattr(sp, "_create_night_llm", lambda: night)
    monkeypatch.setattr(sp, "_create_verifier_llm", lambda: verifier)

    await sp.process_dialog_summarization(session)

    prompt = night.calls[0]["system_prompt"]
    assert "Я" in prompt
    assert "first person" in prompt.lower()
    assert "neutral" in prompt.lower()
    # Glossary (spec 4) is still part of the same prompt.
    assert "promotion" in prompt
    assert "продвижение" in prompt
    assert "Russian only" in prompt


async def test_night_verifier_allows_first_person_and_code_added_prefix(
        sleep_env, utc_local, monkeypatch):
    env = sleep_env
    _seed_chunks(10)
    env.seed_messages(12)
    session = env.begin_sleep()
    night = ScriptedLLM([_summary_response(env, "Я обсудила тему alpha.")])
    verifier = ScriptedLLM([_approve_response(env)])
    monkeypatch.setattr(sp, "_create_night_llm", lambda: night)
    monkeypatch.setattr(sp, "_create_verifier_llm", lambda: verifier)

    await sp.process_dialog_summarization(session)

    verify_prompt = verifier.calls[0]["system_prompt"]
    lowered = verify_prompt.lower()
    assert "first person" in lowered
    assert "neutral" in lowered
    assert "factual" in lowered
    # The code-added prefix must not be a reason to reject.
    assert "do not reject" in lowered
    assert "prefix" in lowered


def test_save_dialog_summary_tool_description_allows_first_person_neutral():
    from core.sleep.tools import get_dialog_summarization_tools

    tools = get_dialog_summarization_tools()
    tool = next(t for t in tools if t["function"]["name"] == "save_dialog_summary")
    desc = tool["function"]["parameters"]["properties"]["summary_text"]["description"]
    lowered = desc.lower()
    assert "first person" in lowered
    assert "neutral" in lowered
    assert "factual" in lowered
    assert "prefix" in lowered


async def test_daytime_compress_stores_prefixed_summary(sleep_env, utc_local, monkeypatch):
    """Daytime path (core/bot._compress_context): same prefix, same style rules."""
    import core.bot as bot_module

    env = sleep_env
    rows = env.seed_messages(10, prefix="alpha long message for daytime compression")
    _set_created_at(rows, "2026-01-02 12:00:00")

    captured = {}

    class FakeSummarizer:
        async def generate(self, messages, max_tokens=None, temperature=None):  # noqa: ARG002
            captured["prompt"] = messages[0]["content"]
            return {"thoughts": "Я обсудила с user-1 тему alpha."}

    class FakeBot:
        summarizer_llm = FakeSummarizer()

        async def _ensure_summarizer(self):
            return None

    monkeypatch.setattr(settings, "CONTEXT_MAX_TOKENS", 10)
    monkeypatch.setattr(settings, "CONTEXT_SUMMARIZE_CHUNK_TOKENS", 10)

    await bot_module.MilaUserbot._compress_context(FakeBot(), CHAT)

    stored = [s["content"] for s in env.summaries()]
    assert stored == ["[2026-01-02] Я обсудила с user-1 тему alpha."]
    prompt = captured["prompt"]
    assert "Я" in prompt
    assert "first person" in prompt.lower()
    assert "neutral" in prompt.lower()
    assert "продвижение" in prompt  # glossary still enforced, daytime side


async def test_prefixed_cjk_summary_is_still_rejected(sleep_env, utc_local):
    """The ASCII prefix does not smuggle CJK past the guard (spec 4)."""
    env = sleep_env
    _reseed(env, "2026-01-02 12:00:00")
    session = env.begin_sleep()

    result = await env.stage_summary(
        session, text="[2026-01-02] user-1 обсуждал 测试 alpha", claimed=12)

    assert result["content"].startswith("ERROR")
    assert env.summaries() == []
    assert env.staging(session) == []
    assert env.summary_log(session) == []


async def test_prefixed_russian_summary_with_glossary_body_is_accepted(
        sleep_env, utc_local):
    """A prefixed summary whose body follows the glossary is accepted as-is."""
    env = sleep_env
    _reseed(env, "2026-01-02 12:00:00")
    session = env.begin_sleep()
    text = "[2026-01-02] Я обсудила продвижение alpha."

    result = await env.stage_summary(session, text=text, claimed=12)

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries()] == [text]
    assert _PREFIX_RE.match(text)

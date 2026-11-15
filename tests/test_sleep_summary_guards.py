"""Dialog-summary guards: real messages_removed (S9), no CJK (S10), glossary (S11).

Expectations come from the SPEC table in TODO.md. The LLM is a scripted fake;
no network is touched.
"""

import json
import re

import pytest

import core.sleep.processes as sp
from core import db as mila_db

CHAT = "chat-1"
USER = "user-1"

# Independent CJK detector for assertions (kana, CJK ideographs, hangul).
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _has_cjk(text):
    return _CJK.search(text or "") is not None


def _snapshot(rows):
    return [(r["id"], r["content"], r["type"]) for r in rows]


def _seed_chunks(n=10):
    """Chunk rows make the chat count as an active dialog (SUMMARIZE_MIN_MESSAGES)."""
    for i in range(n):
        mila_db.add_chunk(CHAT, USER, f"alpha chunk {i}")


class ScriptedLLM:
    """Returns scripted responses in order; the last one repeats."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def generate(self, messages, system_prompt, tools, tool_choice,
                       max_tokens, temperature):  # noqa: ARG002
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "system_prompt": system_prompt,
        })
        index = min(len(self.calls), len(self._responses)) - 1
        return self._responses[index]


def _install_llms(monkeypatch, night, verifier):
    monkeypatch.setattr(sp, "_create_night_llm", lambda: night)
    monkeypatch.setattr(sp, "_create_verifier_llm", lambda: verifier)


def _summary_response(env, text, claimed=12):
    return {"tool_calls": [env.call(
        "save_dialog_summary", chat_id=CHAT, summary_text=text, message_count=claimed)]}


def _approve_response(env):
    return {"tool_calls": [env.call("approve_changes", notes="looks fine")]}


# ---------------------------------------------------------------------------
# S9: messages_removed is the real rowcount, never the model's claim
# ---------------------------------------------------------------------------

async def test_messages_removed_equals_real_deleted_rows_not_model_claim(sleep_env):
    env = sleep_env
    env.seed_messages(12)
    session = env.begin_sleep()

    result = await env.stage_summary(session, claimed=39)

    log = env.summary_log(session)
    assert len(log) == 1
    assert log[0]["messages_removed"] == 12
    assert "12 messages" in result["content"]
    assert "39" not in result["content"]
    staged = [r for r in env.staging(session) if r["target_table"].startswith("context_buffer")]
    assert len(staged) == 1
    assert json.loads(staged[0]["payload"])["messages_removed"] == 12


async def test_messages_removed_is_zero_when_nothing_was_deleted(sleep_env):
    env = sleep_env
    session = env.begin_sleep()

    await env.stage_summary(session, chat_id="chat-empty", claimed=5)

    log = env.summary_log(session)
    assert len(log) == 1
    assert log[0]["messages_removed"] == 0


# ---------------------------------------------------------------------------
# S10: summary must not contain CJK; CJK is rejected before anything is staged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "user-1, 测试 alpha note",
    "こんにちは, user-1 обсудил alpha",
    "안녕하세요 user-1 обсудил alpha",
    "user-1 обсудил alpha 你好",
])
async def test_executor_rejects_cjk_summary_without_staging_anything(sleep_env, text):
    env = sleep_env
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()

    result = await env.stage_summary(session, text=text, claimed=12)

    assert result["content"].startswith("ERROR")
    assert env.staging(session) == []
    assert env.summaries() == []
    assert env.summary_log(session) == []
    assert _snapshot(env.messages()) == before


async def test_russian_summary_with_latin_handles_is_accepted(sleep_env):
    env = sleep_env
    env.seed_messages(12)
    session = env.begin_sleep()

    result = await env.stage_summary(
        session, text="user-1 обсудил alpha @alpha_example и релиз 2026.", claimed=12)

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries()] == [
        "user-1 обсудил alpha @alpha_example и релиз 2026."]


async def test_cjk_summary_is_rewritten_in_russian_on_retry(sleep_env, monkeypatch):
    env = sleep_env
    _seed_chunks(10)
    before = env.seed_messages(12)
    session = env.begin_sleep()
    night = ScriptedLLM([
        _summary_response(env, "user-1 обсуждал 测试 alpha"),
        _summary_response(env, "user-1 обсудил продвижение alpha."),
    ])
    verifier = ScriptedLLM([_approve_response(env)])
    _install_llms(monkeypatch, night, verifier)

    ok, _message = await sp.process_dialog_summarization(session)

    assert ok is True
    # The retry was requested with an explicit Russian-only instruction.
    assert len(night.calls) == 2
    retry_messages = [m["content"] for m in night.calls[1]["messages"]]
    assert any("Russian" in m for m in retry_messages)
    summaries = env.summaries()
    assert [s["content"] for s in summaries] == ["user-1 обсудил продвижение alpha."]
    assert not any(_has_cjk(s["content"]) for s in summaries)
    assert env.messages() == []
    assert len(before) == 12
    log = env.summary_log(session)
    assert [r["messages_removed"] for r in log] == [12]
    assert log and not any(_has_cjk(r["summary_text"]) for r in log)


async def test_persistently_chinese_summary_is_skipped_not_saved(sleep_env, monkeypatch):
    env = sleep_env
    _seed_chunks(10)
    before = _snapshot(env.seed_messages(12))
    session = env.begin_sleep()
    night = ScriptedLLM([_summary_response(env, "user-1 обсуждал 测试 alpha")])
    verifier = ScriptedLLM([_approve_response(env)])
    _install_llms(monkeypatch, night, verifier)

    ok, _message = await sp.process_dialog_summarization(session)

    assert ok is True
    assert env.summaries() == []
    assert _snapshot(env.messages()) == before
    assert env.staging(session) == []
    assert env.summary_log(session) == []


# ---------------------------------------------------------------------------
# S11: summarizer prompt translates "promotion" as продвижение, never сброс
# ---------------------------------------------------------------------------

async def test_summarizer_prompt_maps_promotion_to_advancement_not_reset(
        sleep_env, monkeypatch):
    env = sleep_env
    _seed_chunks(10)
    env.seed_messages(12)
    session = env.begin_sleep()
    night = ScriptedLLM([_summary_response(env, "user-1 обсудил продвижение alpha.")])
    verifier = ScriptedLLM([_approve_response(env)])
    _install_llms(monkeypatch, night, verifier)

    await sp.process_dialog_summarization(session)

    prompt = night.calls[0]["system_prompt"]
    glossary_lines = [line for line in prompt.splitlines() if "promotion" in line]
    assert glossary_lines, "prompt must contain a glossary entry for 'promotion'"
    line = glossary_lines[0]
    assert "продвижение" in line
    # «сброс» may only appear as an explicit prohibition ("never «сброс»").
    for match in re.finditer("сброс", line):
        assert "never" in line[:match.start()].lower()


async def test_summarizer_prompt_forbids_chinese_output(sleep_env, monkeypatch):
    env = sleep_env
    _seed_chunks(10)
    env.seed_messages(12)
    session = env.begin_sleep()
    night = ScriptedLLM([_summary_response(env, "user-1 обсудил продвижение alpha.")])
    verifier = ScriptedLLM([_approve_response(env)])
    _install_llms(monkeypatch, night, verifier)

    await sp.process_dialog_summarization(session)

    prompt = night.calls[0]["system_prompt"]
    assert "Russian only" in prompt
    assert "Chinese" in prompt


# ---------------------------------------------------------------------------
# M3: a summarization executor is bound to the dialog it was created for
# ---------------------------------------------------------------------------

OTHER_CHAT = "chat-2"


def _bound_executor(session_id, expected_chat_id=CHAT):
    return sp.StagingExecutor(
        session_id, "dialog_summarization", expected_chat_id=expected_chat_id)


async def test_summary_for_foreign_chat_is_rejected_and_deletes_nothing(sleep_env):
    env = sleep_env
    own_before = _snapshot(env.seed_messages(12, chat_id=CHAT))
    foreign_before = _snapshot(env.seed_messages(12, prefix="beta message", chat_id=OTHER_CHAT))
    assert len(own_before) == 12 and len(foreign_before) == 12
    session = env.begin_sleep()

    result = await _bound_executor(session).execute_tool_call(env.call(
        "save_dialog_summary", chat_id=OTHER_CHAT,
        summary_text="alpha summary", message_count=12))

    assert result["content"].startswith("ERROR")
    assert env.staging(session) == []
    assert env.summary_log(session) == []
    assert env.summaries(OTHER_CHAT) == []
    assert env.summaries(CHAT) == []
    assert _snapshot(env.messages(OTHER_CHAT)) == foreign_before
    assert _snapshot(env.messages(CHAT)) == own_before


async def test_summary_for_bound_chat_is_accepted(sleep_env):
    env = sleep_env
    env.seed_messages(12, chat_id=CHAT)
    session = env.begin_sleep()

    result = await _bound_executor(session).execute_tool_call(env.call(
        "save_dialog_summary", chat_id=CHAT,
        summary_text="alpha summary", message_count=12))

    assert not result["content"].startswith("ERROR")
    assert [s["content"] for s in env.summaries(CHAT)] == ["alpha summary"]
    assert env.messages(CHAT) == []
    assert [r["messages_removed"] for r in env.summary_log(session)] == [12]


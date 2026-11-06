"""search_memory must not inject the same fact more than once per turn.

The model often fires several overlapping searches in a single iteration.
Every result used to be injected verbatim, so the same fact could appear
several times in the prompt.
"""

from types import SimpleNamespace

import pytest

import core.pipeline as pipeline_module
from core import db as core_db
from core.pipeline import ThinkPipeline
from core.tools.executor import ToolExecutor


class _BrokenVector:
    """Force the SQLite fallback: deterministic, no embeddings/FAISS needed."""

    def refresh_if_changed(self):
        return False

    def search(self, *_args, **_kwargs):
        raise RuntimeError("faiss unavailable")


class _FailingEmbedder:
    async def embed(self, texts):
        raise RuntimeError("embedding backend unavailable")


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "mila.db"))
    core_db.init_db()
    core_db.ensure_user_exists("user-1", "User")
    core_db.ensure_chat_exists("chat-1", "User")
    core_db.add_memory(user_id="user-1", content="alpha fact", importance=0.9)
    core_db.add_memory(user_id="user-1", content="beta fact", importance=0.5)
    return tmp_path


def _executor(seen):
    return ToolExecutor(
        text_memory=_BrokenVector(),
        embedding_client=_FailingEmbedder(),
        current_user_id="user-1",
        chat_id="chat-1",
        seen_memories=seen,
    )


async def _search(executor, query):
    return await executor.execute_action(
        {"action_type": "search_memory", "params": {"query": query}})


async def test_shared_seen_set_drops_repeated_facts(memory_db):
    seen: set[str] = set()

    first = await _search(_executor(seen), "alpha")
    assert any(r["content"] == "alpha fact" for r in first["results"])

    second = await _search(_executor(seen), "fact")
    assert all(r["content"] != "alpha fact" for r in second["results"])
    assert "alpha fact" not in second["context_addon"]
    # The second fact was not shown yet, so it must still come through.
    assert any(r["content"] == "beta fact" for r in second["results"])


async def test_all_duplicates_produce_no_new_facts_message(memory_db):
    seen: set[str] = set()

    await _search(_executor(seen), "fact")
    repeated = await _search(_executor(seen), "alpha")

    assert repeated["results"] == []
    assert repeated["context_addon"] == ToolExecutor._duplicate_context()


async def test_searches_without_shared_set_are_independent(memory_db):
    """A new turn (fresh seen set) may re-surface a fact."""
    first = await _search(_executor(None), "alpha")
    second = await _search(_executor(None), "alpha")

    assert any(r["content"] == "alpha fact" for r in first["results"])
    assert any(r["content"] == "alpha fact" for r in second["results"])


class _FakeLLM:
    default_temperature = 0.7

    def __init__(self, responses):
        self._responses = list(responses)

    async def generate(self, **_kwargs):
        return self._responses.pop(0)


class _DummyTracker:
    def apply_action_effect(self, *_args, **_kwargs):
        pass


def _tool_call(name, arguments):
    return SimpleNamespace(
        id=f"call-{name}",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_pipeline_shares_seen_set_between_searches(monkeypatch):
    """Every search within one turn gets the same seen_memories set."""
    monkeypatch.setattr(pipeline_module, "get_needs_tracker", lambda: _DummyTracker())
    monkeypatch.setattr(pipeline_module, "HAS_OBSERVATORY", False)
    monkeypatch.setattr(pipeline_module, "_get_presence_state",
                        lambda: {"state": "dialog", "focus_chat_id": "chat-1"})
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system prompt")
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)

    responses = [
        {
            "thoughts": "",
            "tool_calls": [
                _tool_call("search_memory", '{"query": "alpha"}'),
                _tool_call("search_memory", '{"query": "beta"}'),
            ],
            "finish_reason": "tool_calls",
            "reasoning": "",
        },
        {
            "thoughts": "ok",
            "tool_calls": None,
            "finish_reason": "stop",
            "reasoning": "",
        },
    ]
    fake_llm = _FakeLLM(responses)
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake_llm))

    pipeline = ThinkPipeline(None, None, "user-1", "chat-1")
    monkeypatch.setattr(pipeline, "_attach_media",
                        lambda messages, media, default_chat_id=None: messages)
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_current_tool_defs",
                        lambda include_proactive=False, state=None: [{"type": "function"}])

    seen_sets = []

    async def fake_execute(action, user_id=None, chat_id=None, seen_memories=None):
        seen_sets.append(seen_memories)
        return {
            "type": "search_memory",
            "query": action["params"]["query"],
            "results": [],
            "context_addon": "[Search results in memory:]\n- alpha fact",
            "status": "searched",
            "source": "faiss",
            "fallback": False,
        }

    monkeypatch.setattr(pipeline, "_execute_single_action", fake_execute)

    events = [event async for event in pipeline._run_stream(
        "what do you remember?", [], None,
        request_user_id="user-1", request_chat_id="chat-1")]

    assert events[-1]["kind"] == "reply_bubbles"
    assert len(seen_sets) == 2
    assert seen_sets[0] is not None
    assert seen_sets[0] is seen_sets[1]

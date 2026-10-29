"""Reasoning is collected from the LLM step and persisted in pipeline logs."""

import json

import pytest

import core.pipeline as pipeline_module
from core.pipeline import ThinkPipeline


class _DummyTracker:
    def apply_action_effect(self, *args, **kwargs):
        pass


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(pipeline_module, "get_needs_tracker", lambda: _DummyTracker())
    return ThinkPipeline(None, None, "user-1", "chat-1")


def _fake_run_stream(*args, **kwargs):
    async def gen():
        yield {
            "kind": "llm",
            "iteration": 1,
            "content": "visible reply",
            "reasoning": "hidden chain of thought",
            "tool_calls": [],
            "finish_reason": "stop",
        }
        yield {"kind": "silence", "reason": "test"}

    return gen()


@pytest.mark.asyncio
async def test_process_message_collects_reasoning(pipeline, monkeypatch):
    captured = {}

    async def fake_save(result, *args, **kwargs):
        captured.update(result)

    monkeypatch.setattr(pipeline, "_run_stream", _fake_run_stream)
    monkeypatch.setattr(pipeline, "_save_log", fake_save)

    await pipeline.process_message("hi", [], [])

    assert captured["raw_llm_responses"][0]["reasoning"] == "hidden chain of thought"


@pytest.mark.asyncio
async def test_process_message_stream_collects_reasoning(pipeline, monkeypatch):
    captured = {}
    events = []

    async def fake_save(result, *args, **kwargs):
        captured.update(result)

    monkeypatch.setattr(pipeline, "_run_stream", _fake_run_stream)
    monkeypatch.setattr(pipeline, "_save_log", fake_save)

    async for event in pipeline.process_message_stream("hi", [], []):
        events.append(event)

    assert events[0]["type"] == "thought"
    assert captured["raw_llm_responses"][0]["reasoning"] == "hidden chain of thought"


@pytest.mark.asyncio
async def test_save_log_persists_reasoning(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module.settings, "LOG_DIR", str(tmp_path))
    pipeline = ThinkPipeline.__new__(ThinkPipeline)
    pipeline.user_id = "user-1"
    pipeline.chat_id = "chat-1"

    await pipeline._save_log(
        {
            "success": True,
            "raw_llm_responses": [
                {
                    "iteration": 1,
                    "thoughts": "visible reply",
                    "reasoning": "hidden chain of thought",
                    "tool_calls": [],
                    "finish_reason": "stop",
                }
            ],
        },
        "hi",
        request_user_id="user-1",
        request_chat_id="chat-1",
    )

    files = list(tmp_path.glob("pipeline_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["raw_llm_responses"][0]["reasoning"] == "hidden chain of thought"

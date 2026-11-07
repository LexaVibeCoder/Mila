"""Unit tests for the tool-calling tester agent in test_tg_bot_dialogue.py.

No Telegram, no network: the transport and LLM are fakes.
"""

from __future__ import annotations

import importlib.util
import types as pytypes
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _load_module():
    """Load the root E2E script without making pytest collect it."""
    spec = importlib.util.spec_from_file_location(
        "tg_bot_dialogue_under_test", ROOT / "test_tg_bot_dialogue.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool_call(name: str, arguments: str):
    return pytypes.SimpleNamespace(
        function=pytypes.SimpleNamespace(name=name, arguments=arguments)
    )


class FakeTransport:
    target_id = 1

    def __init__(self, replies: list[list[str]] | None = None):
        self.sent: list[str] = []
        self.replies = list(replies or [])
        self.latencies: list[float] = []
        self.has_pending = False

    async def send(self, text: str) -> int:
        self.sent.append(text)
        self.has_pending = True
        return len(self.sent)

    async def wait_for_reply(self, timeout: float, settle: float):
        self.has_pending = False
        if not self.replies:
            return None
        return self.replies.pop(0)


class FakeLLM:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)

    async def generate(self, **_kwargs):
        if not self.responses:
            return {"tool_calls": [], "thoughts": ""}
        return self.responses.pop(0)


def _agent(transport: FakeTransport, **overrides):
    mod = _load_module()
    kwargs = dict(
        scenario="all",
        goal=None,
        max_turns=6,
        max_steps=8,
        timeout=5.0,
        settle=0.0,
    )
    kwargs.update(overrides)
    return mod.TesterAgent(transport, **kwargs)


async def test_agent_sends_waits_and_finishes():
    transport = FakeTransport([["alpha reply"]])
    agent = _agent(transport)
    agent.llm = FakeLLM(
        [
            {
                "tool_calls": [
                    _tool_call("send_message", '{"text": "alpha question"}'),
                    _tool_call("wait_for_reply", "{}"),
                ],
                "thoughts": "",
            },
            {
                "tool_calls": [_tool_call("finish", '{"reason": "alpha done"}')],
                "thoughts": "",
            },
        ]
    )

    await agent.run()

    assert transport.sent == ["alpha question"]
    assert agent.finished is True
    assert agent.finish_reason == "alpha done"
    assert [(item["role"], item["text"]) for item in agent.transcript] == [
        ("tester", "alpha question"),
        ("mila", "alpha reply"),
    ]
    assert agent.stats()["messages_sent"] == 1
    assert agent.stats()["messages_received"] == 1


async def test_finish_captures_unwaited_reply():
    transport = FakeTransport([["final alpha reply"]])
    agent = _agent(transport)
    agent.llm = FakeLLM(
        [
            {
                "tool_calls": [
                    _tool_call("send_message", '{"text": "final alpha question"}'),
                    _tool_call("finish", '{"reason": "alpha done"}'),
                ],
                "thoughts": "",
            }
        ]
    )

    await agent.run()

    assert agent.received == 1
    assert agent.transcript[-1]["role"] == "mila"
    assert agent.transcript[-1]["text"] == "final alpha reply"


async def test_turn_limit_blocks_extra_sends():
    transport = FakeTransport()
    agent = _agent(transport, max_turns=1)

    first = await agent._send("alpha")
    second = await agent._send("beta")

    assert transport.sent == ["alpha"]
    assert "Отправлено" in first
    assert "Лимит" in second


async def test_timeout_is_counted():
    transport = FakeTransport()  # no replies
    agent = _agent(transport, timeout=0.01, settle=0.0)

    result = await agent._wait(None)

    assert agent.timeouts == 1
    assert "не ответила" in result


def test_non_text_content_gets_placeholder():
    mod = _load_module()
    sticker = pytypes.SimpleNamespace(
        text="", content=type("MessageSticker", (), {})()
    )
    unknown = pytypes.SimpleNamespace(
        text="", content=type("MessageSomethingNew", (), {})()
    )

    assert mod._content_hint(sticker) == "[стикер]"
    assert mod._content_hint(unknown) == "[MessageSomethingNew]"


def test_custom_scenario_requires_goal_in_prompt():
    transport = FakeTransport()
    agent = _agent(transport, scenario="custom", goal="alpha goal")

    assert agent._scenario_text() == "alpha goal"

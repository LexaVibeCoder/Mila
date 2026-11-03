"""Sleep dialog summarization: language choice and summarizer wiring.

Hermetic: no network, no real DB, no Telegram.
"""

import json
from types import SimpleNamespace

import pytest

from core.sleep import processes
from core.sleep.processes import (
    MAX_SUMMARIZER_CHARS,
    _clean_summary,
    _create_summarizer_llm,
    _dialog_summary_instructions,
    _summarize_dialog,
    process_dialog_summarization,
)


class _FakeLLM:
    def __init__(self, text: str = "", error: Exception | None = None,
                 max_tokens: int = 600):
        self.text = text
        self.error = error
        self.default_max_tokens = max_tokens
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"thoughts": self.text}


class _FakeToolCall:
    def __init__(self, name: str, arguments: dict):
        self.function = SimpleNamespace(
            name=name, arguments=json.dumps(arguments, ensure_ascii=False)
        )


class _FakeVerifier:
    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"tool_calls": [_FakeToolCall("approve_changes", {})]}


class _FakeExecutor:
    instances = []

    def __init__(self, session_id, process_type):
        self.session_id = session_id
        self.process_type = process_type
        self.staged = []
        self.skipped = False
        self.staged_ids = []
        self.last_dialog_summary = None
        _FakeExecutor.instances.append(self)

    async def stage_dialog_summary(self, chat_id, summary_text, message_count):
        self.staged.append((chat_id, summary_text, message_count))
        self.last_dialog_summary = summary_text
        return {"role": "system", "content": "staged"}

    async def execute_tool_call(self, tool_call):
        name = tool_call.function.name
        if name == "save_dialog_summary":
            args = json.loads(tool_call.function.arguments)
            await self.stage_dialog_summary(
                args["chat_id"], args["summary_text"], args.get("message_count", 0)
            )
            return {"role": "system", "content": "staged"}
        if name == "approve_changes":
            return {"_approved": True}
        return {"role": "system", "content": "ok"}


def _prepare_dialog(monkeypatch, message_count=12):
    messages = [
        {"type": "message", "content": f"сообщение-{i}", "created_at": "2026-10-01"}
        for i in range(message_count)
    ]
    monkeypatch.setattr(processes, "set_stage", lambda *a, **k: None)
    monkeypatch.setattr(
        processes, "get_active_dialogs_detail",
        lambda: [{"chat_id": "chat-1", "message_count": message_count}],
    )
    # process_dialog_summarization imports get_context locally.
    import core.context.buffer as buffer
    monkeypatch.setattr(buffer, "get_context", lambda *a, **k: messages)
    monkeypatch.setattr(processes, "StagingExecutor", _FakeExecutor)
    monkeypatch.setattr(processes, "_get_night_tool_choice", lambda: "required")
    _FakeExecutor.instances.clear()


def test_create_summarizer_uses_defaults(monkeypatch):
    import Mila_observatory.runtime as runtime_module

    runtime_values = {"summarization.api_base": "https://openrouter.ai/api/v1"}
    monkeypatch.setattr(
        runtime_module.runtime_config, "get",
        lambda key, default=None: runtime_values.get(key, default),
    )

    client = _create_summarizer_llm()

    assert client is not None
    assert client.model == "scaledown/summarize"
    assert client.default_max_tokens == 600


def test_create_summarizer_skips_non_openrouter_base(monkeypatch):
    import Mila_observatory.runtime as runtime_module

    runtime_values = {"summarization.api_base": "http://127.0.0.1:1234/v1"}
    monkeypatch.setattr(
        runtime_module.runtime_config, "get",
        lambda key, default=None: runtime_values.get(key, default),
    )

    assert _create_summarizer_llm() is None


def test_create_summarizer_can_be_disabled(monkeypatch):
    import Mila_observatory.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module.runtime_config, "get",
        lambda key, default=None: "" if key == "summarization.model" else default,
    )

    assert _create_summarizer_llm() is None


def test_russian_dialog_gets_russian_instructions():
    ru = "Пользователь: перенесли деплой на вторник. Ассистент: записал BILL-482."
    en = "User: we moved the deploy to Tuesday. Assistant: noted BILL-482."
    mixed = "Обсудили деплой, billing_invoice_runs и jest.spyOn."

    assert "русском" in _dialog_summary_instructions(ru)
    assert "English" in _dialog_summary_instructions(en)
    assert "русском" in _dialog_summary_instructions(mixed)


def test_clean_summary_trims_silent_truncation():
    good = "Перенесли деплой на вторник. Ревьюер @mara."
    assert _clean_summary(good) == good

    truncated = ("Обсудили перенос деплоя на вторник и миграцию таблицы. "
                 "Проверили ветку fix/invoice-clock-injection и тикет BILL-4")
    cleaned = _clean_summary(truncated)
    assert cleaned.endswith("таблицы.")


async def test_summarize_dialog_uses_russian_prompt_and_truncates_input():
    llm = _FakeLLM("  Короткое резюме.  ")
    text = "Пользователь: тест. " * 4000

    summary = await _summarize_dialog(llm, text)

    assert summary == "Короткое резюме."
    call = llm.calls[0]
    assert "русском" in call["system_prompt"]
    assert call["temperature"] == 0.0
    assert len(call["messages"][0]["content"]) <= MAX_SUMMARIZER_CHARS


async def test_process_uses_dedicated_summarizer(monkeypatch):
    _prepare_dialog(monkeypatch)
    summarizer = _FakeLLM("Обсудили деплой и сроки.")
    night = _FakeLLM()
    verifier = _FakeVerifier()

    monkeypatch.setattr(processes, "_create_night_llm", lambda: night)
    monkeypatch.setattr(processes, "_create_verifier_llm", lambda: verifier)
    monkeypatch.setattr(processes, "_create_summarizer_llm", lambda: summarizer)

    ok, message = await process_dialog_summarization("session-1")

    assert ok
    assert "1/1" in message
    executor = _FakeExecutor.instances[-1]
    assert executor.staged == [("chat-1", "Обсудили деплой и сроки.", 12)]
    assert summarizer.calls
    assert not night.calls
    assert "Обсудили деплой и сроки." in verifier.calls[0]["system_prompt"]


async def test_process_falls_back_to_night_llm(monkeypatch):
    _prepare_dialog(monkeypatch)
    summarizer = _FakeLLM(error=RuntimeError("provider down"))

    legacy_tool_call = _FakeToolCall("save_dialog_summary", {
        "chat_id": "chat-1",
        "summary_text": "Резюме от ночного LLM.",
        "message_count": 12,
    })
    night = _FakeLLM()

    async def night_generate(**kwargs):
        night.calls.append(kwargs)
        return {"tool_calls": [legacy_tool_call]}

    night.generate = night_generate

    monkeypatch.setattr(processes, "_create_night_llm", lambda: night)
    monkeypatch.setattr(processes, "_create_verifier_llm", lambda: _FakeVerifier())
    monkeypatch.setattr(processes, "_create_summarizer_llm", lambda: summarizer)

    ok, message = await process_dialog_summarization("session-1")

    assert ok
    assert "1/1" in message
    executor = _FakeExecutor.instances[-1]
    assert executor.staged == [("chat-1", "Резюме от ночного LLM.", 12)]
    assert night.calls


async def test_process_skips_short_dialog(monkeypatch):
    _prepare_dialog(monkeypatch, message_count=5)
    summarizer = _FakeLLM("Не должно использоваться.")

    monkeypatch.setattr(processes, "_create_night_llm", lambda: _FakeLLM())
    monkeypatch.setattr(processes, "_create_verifier_llm", lambda: _FakeVerifier())
    monkeypatch.setattr(processes, "_create_summarizer_llm", lambda: summarizer)

    ok, _ = await process_dialog_summarization("session-1")

    assert ok
    assert not summarizer.calls
    assert not _FakeExecutor.instances

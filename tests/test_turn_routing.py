"""Replies follow the global dialog focus, not the notification sender."""

import asyncio
from types import SimpleNamespace

import pytest

import core.delivery as delivery
import core.pipeline as pipeline_module
import core.queue as queue_module
from core.delivery import TurnContext, deliver_messages
from core.pipeline import ThinkPipeline
from core.queue import MessageCoordinate


DIALOG_A = {
    "state": "dialog",
    "focus_chat_id": "10001",
    "focus_user_name": "A",
    "note": "",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


class _FakeLLM:
    default_temperature = 0.7

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _reply(text):
    return {"thoughts": text, "tool_calls": None, "finish_reason": "stop",
            "reasoning": ""}


async def _run(pipeline, turn, user="user-2", chat="20002"):
    return [event async for event in pipeline._run_stream(
        "hi", [], None, request_user_id=user, request_chat_id=chat,
        turn=turn)]


@pytest.mark.asyncio
async def test_dialog_turn_replies_to_focus_not_notification_sender(monkeypatch):
    fake = _FakeLLM([_reply("hello")])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system")
    pipeline = ThinkPipeline(None, None, "user-2", "20002")
    monkeypatch.setattr(pipeline, "_attach_media", lambda m, media, default_chat_id=None: m)
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: None)

    turn = TurnContext(turn_id="turn-1", chat_id="20002", user_id="user-2",
                       presence=DIALOG_A)

    events = await _run(pipeline, turn)

    # The event itself has no destination...
    assert events[-1] == {"kind": "reply_bubbles", "texts": ["hello"]}
    assert turn.resolve_target() == "10001"
    assert turn.resolve_target("20002") is None


@pytest.mark.asyncio
async def test_presence_snapshot_is_not_reread_mid_turn(monkeypatch):
    read_calls = []

    def fake_presence():
        read_calls.append(1)
        return {"state": "rest", "focus_chat_id": None, "updated_at": None}

    prompts = []
    fake = _FakeLLM([_reply("hello")])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    monkeypatch.setattr(pipeline_module, "_get_presence_state", fake_presence)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: prompts.append(k.get("presence")) or "system")
    pipeline = ThinkPipeline(None, None, "user-2", "20002")
    monkeypatch.setattr(pipeline, "_attach_media", lambda m, media, default_chat_id=None: m)
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: None)

    turn = TurnContext(turn_id="turn-1", chat_id="20002", user_id="user-2",
                       presence=DIALOG_A)
    await _run(pipeline, turn)

    # No global presence reads: the snapshot drives prompt/tools.
    assert read_calls == []
    assert prompts and prompts[0]["state"] == "dialog"
    assert prompts[0]["focus_chat_id"] == "10001"
    assert turn.resolve_target() == "10001"


def test_resolve_target_fails_closed_for_foreign_chat():
    turn = TurnContext(turn_id="turn-1", chat_id="20002", user_id="user-2",
                       presence=DIALOG_A)

    assert turn.resolve_target() == "10001"
    assert turn.resolve_target("20002") is None
    assert turn.resolve_target("30003") is None

    turn.allow("20002")
    assert turn.resolve_target() == "20002"
    assert turn.resolve_target("30003") is None


def test_pseudo_owner_has_no_target_without_grant():
    turn = TurnContext(turn_id="turn-1", chat_id="proactive", user_id="proactive",
                       presence={"state": "rest"})

    assert turn.resolve_target() is None
    turn.allow("20002")
    assert turn.resolve_target() == "20002"


@pytest.mark.asyncio
async def test_proactive_turn_uses_open_dialog_snapshot(monkeypatch):
    fake = _FakeLLM([_reply("hello")])
    monkeypatch.setattr(ThinkPipeline, "main_llm", property(lambda self: fake))
    monkeypatch.setattr(pipeline_module, "_max_iterations", lambda: 3)
    monkeypatch.setattr(pipeline_module, "build_main_llm_system_prompt",
                        lambda *a, **k: "system")
    pipeline = ThinkPipeline(None, None, "proactive", "proactive")
    monkeypatch.setattr(pipeline, "_attach_media", lambda m, media, default_chat_id=None: m)
    monkeypatch.setattr(pipeline, "_build_notification_block", lambda *a, **k: None)

    turn = TurnContext(turn_id="turn-1", chat_id="proactive", user_id="proactive",
                       presence=DIALOG_A, proactive=True)

    events = await _run(pipeline, turn, user="proactive", chat="proactive")

    assert events[-1] == {"kind": "reply_bubbles", "texts": ["hello"]}
    assert turn.resolve_target() == "10001"
    assert turn.resolve_target("20002") is None


@pytest.mark.asyncio
async def test_delivery_records_only_to_target(monkeypatch):
    sent = []
    recorded = []

    async def fake_send_text(client, chat_id, text):
        sent.append((str(chat_id), text))

    async def fake_send_typing(client, chat_id):
        return None

    class _Indexer:
        def add_chunk(self, chat_id, user_id, content, **kwargs):
            recorded.append(("chunk", chat_id))
            return "chunk-1"

    monkeypatch.setattr(delivery, "send_text", fake_send_text)
    monkeypatch.setattr(delivery, "send_typing", fake_send_typing)
    monkeypatch.setattr(delivery.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(delivery, "push_message",
                        lambda chat_id, content, **kwargs: recorded.append(("ctx", chat_id)))

    result = await deliver_messages(client=None, target="20002", bubbles=["hello"],
                                    my_user_id="me", indexer=_Indexer())

    assert result.ok and result.sent == 1
    assert sent == [("20002", "hello")]
    assert recorded == [("chunk", "20002"), ("ctx", "20002")]


class _FakePipeline:
    def __init__(self):
        self.user_id = "system"
        self.chat_id = "system"
        self.calls = []

    async def process_message_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type": "reply", "texts": ["hello from B"], "via": "bubbles"}


class _FakeBot:
    def __init__(self):
        self.me = SimpleNamespace(id=42)
        self.pipeline = _FakePipeline()
        self.indexer = SimpleNamespace(
            add_chunk=lambda chat_id, user_id, **kw: f"chunk-{chat_id}")
        self.sent = []
        self.recorded = []

    def _build_histories(self, chat_id):
        return [], []

    def _resolve_roles_in_histories(self, history, chat_id):
        return history

    async def _compress_context(self, chat_id):
        return None


@pytest.mark.asyncio
async def test_queue_turn_sends_to_presence_focus(monkeypatch):
    bot = _FakeBot()
    monkeypatch.setattr(queue_module, "get_presence", lambda: dict(DIALOG_A))
    monkeypatch.setattr(queue_module, "get_user_info", lambda user_id: {})
    monkeypatch.setattr(queue_module, "get_needs_tracker", lambda: SimpleNamespace(
        get_needs=lambda chat_id=None: {"current_hour": 12, "fatigue": 0.0},
        apply_action_effect=lambda *a: None,
    ))
    monkeypatch.setattr("core.sleep.manager.is_sleeping", lambda: False)

    async def fake_send_text(client, chat_id, text):
        bot.sent.append((str(chat_id), text))

    async def fake_send_typing(client, chat_id):
        return None

    monkeypatch.setattr(delivery, "send_text", fake_send_text)
    monkeypatch.setattr(delivery, "send_typing", fake_send_typing)
    monkeypatch.setattr(delivery.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(delivery, "push_message",
                        lambda chat_id, content, **kwargs: bot.recorded.append(chat_id))

    coordinator = MessageCoordinate(bot)
    coordinator._events["20002"] = asyncio.Event()
    coordinator._pending["20002"] = ["hello"]
    coordinator._chat_user["20002"] = "user-2"

    await coordinator._generate_for_chat(client=None, chat_id="20002")

    assert bot.sent == [("10001", "hello from B")]
    assert bot.recorded == ["10001"]
    turn = bot.pipeline.calls[0]["turn"]
    assert turn.chat_id == "20002"
    assert turn.presence["focus_chat_id"] == "10001"


@pytest.mark.asyncio
@pytest.mark.parametrize(("incoming", "should_interrupt"), [
    ("10001", True),   # current dialog
    ("20002", True),   # source of the notification
    ("30003", False),  # unrelated chat
])
async def test_queue_interrupts_only_focus_or_notification(monkeypatch, incoming, should_interrupt):
    bot = _FakeBot()
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(**kwargs):
        entered.set()
        await release.wait()
        yield {"type": "reply", "texts": ["response"], "via": "bubbles"}

    bot.pipeline.process_message_stream = stream
    monkeypatch.setattr(queue_module, "get_presence", lambda: dict(DIALOG_A))
    monkeypatch.setattr(queue_module, "get_user_info", lambda user_id: {})
    monkeypatch.setattr(queue_module, "get_needs_tracker", lambda: SimpleNamespace(
        get_needs=lambda chat_id=None: {"current_hour": 12, "fatigue": 0.0},
        apply_action_effect=lambda *a: None))
    monkeypatch.setattr("core.sleep.manager.is_sleeping", lambda: False)

    async def send(client, chat_id, text):
        bot.sent.append((str(chat_id), text))

    async def typing(client, chat_id):
        pass

    monkeypatch.setattr(delivery, "send_text", send)
    monkeypatch.setattr(delivery, "send_typing", typing)
    monkeypatch.setattr(delivery.random, "uniform", lambda *a: 0)
    monkeypatch.setattr(delivery, "push_message", lambda *a, **k: None)
    coordinator = MessageCoordinate(bot)
    coordinator._events["20002"] = asyncio.Event()
    coordinator._pending["20002"] = ["initial notification"]
    coordinator._chat_user["20002"] = "user-2"
    coordinator._batch_media["20002"] = {"image_data_urls": ["synthetic-image"]}
    task = asyncio.create_task(coordinator._generate_for_chat(None, "20002"))
    await entered.wait()
    coordinator._revisions[incoming] = 1
    if incoming == "20002":
        coordinator._pending["20002"].append("additional notification")
        coordinator._events["20002"].set()
    release.set()
    assert await task is should_interrupt
    if should_interrupt:
        assert not bot.sent
        assert coordinator._pending["20002"][0] == "initial notification"
        assert coordinator._batch_media["20002"]["image_data_urls"] == ["synthetic-image"]
        if incoming == "20002":
            assert coordinator._pending["20002"] == ["initial notification", "additional notification"]
    else:
        assert bot.sent == [("10001", "response")]


@pytest.mark.asyncio
async def test_workers_serialize_turns_without_blocking_incoming(monkeypatch):
    bot = _FakeBot()
    first_started, release = asyncio.Event(), asyncio.Event()
    started = []

    async def stream(**kwargs):
        started.append(kwargs["request_chat_id"])
        if len(started) == 1:
            first_started.set()
            await release.wait()
        yield {"type": "silence", "reason": "test"}

    bot.pipeline.process_message_stream = stream
    monkeypatch.setattr(queue_module, "get_presence", lambda: dict(DIALOG_A))
    monkeypatch.setattr(queue_module, "get_user_info", lambda user_id: {})
    monkeypatch.setattr(queue_module, "get_needs_tracker", lambda: SimpleNamespace(
        get_needs=lambda chat_id=None: {"current_hour": 12, "fatigue": 0.0},
        apply_action_effect=lambda *a: None))
    monkeypatch.setattr("core.sleep.manager.is_sleeping", lambda: False)
    monkeypatch.setattr(queue_module.settings, "ENABLE_TYPING_WAIT", False)
    coordinator = MessageCoordinate(bot)
    for chat in ("10001", "20002"):
        coordinator._events[chat] = asyncio.Event()
        coordinator._pending[chat] = ["message"]
        coordinator._chat_user[chat] = "user"
    first = asyncio.create_task(coordinator._worker(None, "10001"))
    await first_started.wait()
    second = asyncio.create_task(coordinator._worker(None, "20002"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == ["10001"]
    assert coordinator._pending["20002"] == ["message"]
    release.set()
    await asyncio.gather(first, second)
    assert started == ["10001", "20002"]


@pytest.mark.asyncio
async def test_external_presence_change_blocks_stale_reply(monkeypatch):
    bot = _FakeBot()
    entered, release = asyncio.Event(), asyncio.Event()
    presence = dict(DIALOG_A)

    async def stream(**kwargs):
        entered.set()
        await release.wait()
        yield {"type": "reply", "texts": ["response"], "via": "bubbles"}

    bot.pipeline.process_message_stream = stream
    monkeypatch.setattr(queue_module, "get_presence", lambda: dict(presence))
    monkeypatch.setattr(queue_module, "get_user_info", lambda user_id: {})
    monkeypatch.setattr(queue_module, "get_needs_tracker", lambda: SimpleNamespace(
        get_needs=lambda chat_id=None: {"current_hour": 12, "fatigue": 0.0},
        apply_action_effect=lambda *a: None))
    monkeypatch.setattr("core.sleep.manager.is_sleeping", lambda: False)
    coordinator = MessageCoordinate(bot)
    coordinator._events["10001"] = asyncio.Event()
    coordinator._pending["10001"] = ["message"]
    task = asyncio.create_task(coordinator._generate_for_chat(None, "10001"))
    await entered.wait()
    presence.update(state="dialog", focus_chat_id="20002", updated_at="new")
    release.set()
    assert await task is True
    assert coordinator._pending["10001"] == ["message"]
    assert bot.sent == []


@pytest.mark.asyncio
async def test_presence_change_during_typing_delay_blocks_first_bubble(monkeypatch):
    bot = _FakeBot()
    presence = dict(DIALOG_A)
    monkeypatch.setattr(queue_module, "get_presence", lambda: dict(presence))
    monkeypatch.setattr(queue_module, "get_user_info", lambda user_id: {})
    monkeypatch.setattr(queue_module, "get_needs_tracker", lambda: SimpleNamespace(
        get_needs=lambda chat_id=None: {"current_hour": 12, "fatigue": 0.0},
        apply_action_effect=lambda *a: None))
    monkeypatch.setattr("core.sleep.manager.is_sleeping", lambda: False)

    async def typing(client, chat_id):
        presence.update(focus_chat_id="20002", updated_at="new")

    async def send(client, chat_id, text):
        bot.sent.append(text)

    monkeypatch.setattr(delivery, "send_typing", typing)
    monkeypatch.setattr(delivery, "send_text", send)
    monkeypatch.setattr(delivery.random, "uniform", lambda *a: 0)
    coordinator = MessageCoordinate(bot)
    coordinator._events["10001"] = asyncio.Event()
    coordinator._pending["10001"] = ["message"]
    assert await coordinator._generate_for_chat(None, "10001") is True
    assert bot.sent == []
    assert coordinator._pending["10001"] == ["message"]

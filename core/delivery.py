"""Turn-local routing and the single outbound delivery gateway.

Ordinary speech goes to the current dialog focus, never to the notification
sender by default. Explicit one-shot notes may grant a different target.

Keeping the chunk/context writes here means every sent bubble is recorded in
exactly the chat it was delivered to, and only after a successful send.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from core.context.buffer import push_message
from core.memory.indexer import get_indexer
from core.tg_adapter import send_text, send_typing

# Identifiers that are not Telegram chats (pipeline/queue pseudo-chats).
PSEUDO_CHATS = frozenset({"", "proactive", "system"})


def is_chat_target(value: str | None) -> bool:
    """True when value can be passed to TDLib as a chat id."""
    raw = str(value or "").strip()
    return bool(raw) and raw not in PSEUDO_CHATS and raw.lstrip("-").isdigit()


@dataclass
class TurnContext:
    """Immutable identity of a turn + explicit delivery grants.

    ``chat_id`` and ``user_id`` identify the notification. ``presence`` is
    snapshotted at turn start and refreshed after this turn's transitions.
    Callers check it against global state before delivering.
    """

    turn_id: str
    chat_id: str
    user_id: str
    presence: dict = field(default_factory=dict)
    proactive: bool = False
    _granted: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.set_presence(self.presence)

    def set_presence(self, presence: dict) -> None:
        """Refresh the turn's focus after its own successful transition."""
        self.presence = dict(presence)
        self._granted.clear()
        if self.mode == "dialog":
            self.allow(self.presence.get("focus_chat_id"))

    @property
    def mode(self) -> str:
        return str(self.presence.get("state") or "rest")

    @property
    def presence_epoch(self) -> Optional[str]:
        return self.presence.get("updated_at")

    def allow(self, chat_id: str | None) -> None:
        """Explicitly allow one more delivery target for this turn."""
        cid = str(chat_id or "").strip()
        if is_chat_target(cid) and cid not in self._granted:
            self._granted.append(cid)

    def resolve_target(self, requested: str | None = None) -> Optional[str]:
        """Return a valid target, or None when the request is not allowed.

        The focus or the last explicit one-shot grant wins; the notification
        sender is not an implicit destination.
        """
        if requested is not None:
            raw = str(requested).strip()
            if raw in self._granted:
                return raw if is_chat_target(raw) else None
            return None
        if self._granted:
            return self._granted[-1]
        return None


@dataclass
class DeliveryResult:
    ok: bool
    target: Optional[str]
    sent: int = 0
    error: Optional[str] = None


async def deliver_messages(
    *,
    client,
    target: str,
    bubbles: list[str],
    my_user_id: str,
    indexer=None,
    via: str = "bubbles",
    full_response: str = "",
    before_bubble: Callable[[int], Awaitable[bool]] | None = None,
) -> DeliveryResult:
    """Send text bubbles to ``target`` and record each one after the send.

    ``before_bubble(i)`` is an optional async guard called before each bubble
    and again after the typing delay (used for interruption checks);
    returning False aborts the rest of the batch without failing it.
    """
    cleans = [t for t in (bubbles or []) if (t or "").strip()]
    if not cleans:
        return DeliveryResult(ok=False, target=target, error="empty")
    if not is_chat_target(target):
        return DeliveryResult(ok=False, target=target, error="invalid target")

    if indexer is None:
        try:
            indexer = get_indexer()
        except Exception:
            indexer = None

    print(f"\n[SENDING via {via}] {len(cleans)} bubble(s)... → chat {target}")
    if before_bubble is not None and not await before_bubble(0):
        return DeliveryResult(ok=False, target=target, error="interrupted")
    try:
        await send_typing(client, int(target))
    except Exception:
        pass
    await asyncio.sleep(random.uniform(0.3, 1.0))

    sent = 0
    sent_texts: list[str] = []
    for i, clean in enumerate(cleans):
        if i > 0:
            if before_bubble is not None and not await before_bubble(i):
                break
            try:
                await send_typing(client, int(target))
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.3, 1.0))
        if before_bubble is not None and not await before_bubble(i):
            return DeliveryResult(ok=False, target=target, sent=sent, error="interrupted")
        try:
            await send_text(client, int(target), clean)
        except Exception as e:
            print(f"[Send Error] {target}: {e}")
            return DeliveryResult(ok=False, target=target, sent=sent,
                                  error=str(e))

        sent += 1
        sent_texts.append(clean)
        # The message is already delivered: record it in the SAME chat.
        # A storage failure must not trigger a re-send (that would duplicate),
        # so both writes are best-effort and independent.
        chunk_id = None
        if indexer is not None:
            try:
                chunk_id = indexer.add_chunk(
                    target, my_user_id, content=clean,
                    llm_full_response=full_response or clean,
                    token_count=0, type="raw",
                )
            except Exception as e:
                print(f"[Delivery] chunk write failed for {target}: {e}")
        try:
            push_message(target, clean, source_chunk_id=chunk_id,
                         role="assistant")
        except Exception as e:
            print(f"[Delivery] buffer write failed for {target}: {e}")
        print(f"[✓] Bubble {i + 1}/{len(cleans)} sent")

    # Only the explicit one-shot send_message tool goes into the action
    # journal. Ordinary dialog bubbles stay in the chat context; the global
    # journal is about state/location and deliberate outbound notes, not every
    # reply. Record after a real send, not when the tool merely returns ready.
    if sent and via == "send_message_brush_off":
        try:
            from core.action_journal import record_send_message
            record_send_message(target, " ".join(sent_texts))
        except Exception as e:
            print(f"[Delivery] journal write failed for {target}: {e}")

    return DeliveryResult(ok=True, target=target, sent=sent)

"""
Per-chat message batching with debounce + peer typing awareness.

Each chat gets a single worker that:

1. Collects messages pushed to the context buffer during a quiet window.
2. Restarts the window whenever a new message arrives (debounce).
3. If the peer is typing (TDLib updateChatAction) — stops and waits until
   they send the message, stop typing (+grace), or max wait expires.
4. Sends the accumulated messages to the LLM once.
5. If a new message arrives OR typing starts DURING generation — cancels
   the LLM run (and any pending send) and restarts the cycle.
"""

import asyncio
import random
import time

from pytdbot import types

from config import settings
from core.db import (
    ensure_user_exists, ensure_chat_exists, add_chunk, get_user_info
)
from core.context.buffer import push_message
from core.needs.tracker import get_tracker as get_needs_tracker
from core.tg_adapter import (
    normalize_message, send_text, send_typing,
    parse_chat_action_update, is_typing_like_action, is_cancel_action,
)


class MessageCoordinate:
    """
    Owns one task per chat. `handle()` is called on every incoming message and
    simply: saves + wakes the worker. The worker does the batching.
    """

    def __init__(self, bot):
        self.bot = bot
        self._events = {}      # chat_id -> asyncio.Event (wake-up signal)
        self._pending = {}     # chat_id -> list[str] (text accumulated this batch)
        self._tasks = {}       # chat_id -> asyncio.Task
        # Peer typing state: chat_id -> monotonic timestamp of last
        # non-cancel updateChatAction. Expires via TYPING_FRESHNESS_SECONDS.
        self._typing_last: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Peer typing tracking (called from bot's updateChatAction handler)
    # ------------------------------------------------------------------

    def note_typing(self, chat_id: str) -> None:
        """Record that the peer is typing in chat_id right now."""
        self._typing_last[str(chat_id)] = time.monotonic()

    def note_typing_cancel(self, chat_id: str) -> None:
        """Peer explicitly stopped (ChatActionCancel) — clear state."""
        self._typing_last.pop(str(chat_id), None)

    def is_user_typing(self, chat_id: str) -> bool:
        """True if a typing-like action was seen within freshness window."""
        ts = self._typing_last.get(str(chat_id))
        if ts is None:
            return False
        freshness = getattr(settings, "TYPING_FRESHNESS_SECONDS", 6.0)
        if time.monotonic() - ts > freshness:
            # Expired — TDLib stops re-sending typing after ~6s.
            self._typing_last.pop(str(chat_id), None)
            return False
        return True

    async def handle_typing_update(self, client, update, my_id=None) -> None:
        """
        Entry point for TDLib updateChatAction.

        - Ignores our own actions (sender == my_id).
        - Any non-cancel action → note_typing; cancel → note_typing_cancel.
        - Never raises (typing is best-effort).
        """
        try:
            chat_id, sender_user_id, action = parse_chat_action_update(update)
            if chat_id is None:
                return
            if my_id is not None and sender_user_id is not None:
                try:
                    if int(sender_user_id) == int(my_id):
                        return
                except (TypeError, ValueError):
                    pass
            key = str(chat_id)
            if action is None or is_cancel_action(action):
                self.note_typing_cancel(key)
            elif is_typing_like_action(action):
                self.note_typing(key)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public entry point, called from the message handler
    # ------------------------------------------------------------------

    async def handle(self, client, message: types.Message):
        nm = await normalize_message(client, message)
        user_id = nm.user_id
        chat_id = nm.chat_id
        text = nm.text

        # Always save to canonical storage immediately.
        ensure_user_exists(user_id, nm.sender_name)
        ensure_chat_exists(chat_id, nm.chat_name)
        chunk_id = add_chunk(chat_id, user_id, text, token_count=0, type='raw')

        # If Mila is sleeping — just queue the message, no batching.
        from core.sleep.manager import is_sleeping, queue_incoming_message
        if is_sleeping():
            queue_incoming_message(chat_id, user_id, text)
            print(f"[Sleep] Message queued from {user_id[:8]}... in {chat_id[:8]}...")
            return

        # Push to working memory now so it appears in the pending batch.
        push_message(chat_id, text, source_chunk_id=chunk_id)

        # Accumulate text for this batch.
        self._pending.setdefault(chat_id, []).append(text)

        # Wake the worker (or start one if none running).
        event = self._events.setdefault(chat_id, asyncio.Event())
        event.set()

        task = self._tasks.get(chat_id)
        if task is None or task.done():
            self._tasks[chat_id] = asyncio.create_task(
                self._worker(client, chat_id)
            )

    # ------------------------------------------------------------------
    # Worker: owns the batching loop for one chat
    # ------------------------------------------------------------------

    async def _worker(self, client, chat_id: str):
        try:
            while True:
                # 1. Debounce — wait until the user goes quiet.
                await self._debounce(chat_id)

                # 2. If the peer is typing — stop and wait for their message.
                if settings.ENABLE_TYPING_WAIT:
                    await self._wait_typing(chat_id, client)
                    # A real message may have arrived while we waited for
                    # typing to stop — re-debounce instead of generating.
                    event = self._events.get(chat_id)
                    if event is not None and event.is_set():
                        continue

                # 3. Generate for whatever accumulated; restart if new message
                #    arrived or typing started while we were generating.
                restart = await self._generate_for_chat(client, chat_id)
                if restart:
                    continue

                # If a message slipped in after generation finished (e.g. during
                # the task.done() gap in handle()), process it too.
                if self._pending.get(chat_id):
                    continue

                # Nothing left — this batch session is done.
                self._pending.pop(chat_id, None)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            print("[❌ BATCHER ERROR] Unhandled exception in worker:")
            traceback.print_exc()
            raise

    async def _debounce(self, chat_id: str):
        """Wait until no new message arrives for MESSAGE_DEBOUNCE_SECONDS."""
        event = self._events[chat_id]
        while True:
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), settings.MESSAGE_DEBOUNCE_SECONDS)
                # New message arrived during the window → restart the timer.
                continue
            except asyncio.TimeoutError:
                return

    async def _wait_typing(self, chat_id: str, client):
        """
        Full typing support: if the peer is typing, stop and wait until
        they either send the message (event set → caller re-debounces)
        or stop typing + grace period expires, or max wait is reached.

        Fast path: no typing seen → return immediately (no added latency).
        Bounded: never waits longer than TYPING_WAIT_MAX_SECONDS so a user
        who types forever cannot starve the bot.
        """
        event = self._events[chat_id]

        # Fast path — nobody typing, proceed to generation right away.
        if not self.is_user_typing(chat_id):
            return

        max_wait = getattr(settings, "TYPING_WAIT_MAX_SECONDS", 8.0)
        poll_step = getattr(settings, "TYPING_POLL_INTERVAL", 0.5)
        grace = getattr(settings, "TYPING_GRACE_SECONDS", 2.0)

        print("[Typing] peer is typing — waiting for their message...")
        waited = 0.0
        typing_ended_at: float | None = None

        while waited < max_wait:
            if event.is_set():
                # Real message arrived — caller will re-debounce.
                return
            if self.is_user_typing(chat_id):
                typing_ended_at = None
            else:
                # Typing stopped — give the message a grace window to arrive
                # (push → handle() sets event). Then proceed.
                if typing_ended_at is None:
                    typing_ended_at = time.monotonic()
                    print("[Typing] stopped — grace wait for message...")
                if time.monotonic() - typing_ended_at >= grace:
                    return
            await asyncio.sleep(poll_step)
            waited += poll_step

        print("[Typing] max wait reached — proceeding anyway")

    def _should_abort_for_typing(self, chat_id: str) -> bool:
        """True if typing-abort is enabled and the peer is currently typing."""
        if not getattr(settings, "ENABLE_TYPING_WAIT", False):
            return False
        if not getattr(settings, "TYPING_ABORT_GENERATION", True):
            return False
        return self.is_user_typing(chat_id)

    def _interrupted(self, chat_id: str, event: asyncio.Event) -> str | None:
        """
        Unified interruption check: new message or peer typing.
        Returns reason string ('message' / 'typing') or None.
        """
        if event.is_set():
            return "message"
        if self._should_abort_for_typing(chat_id):
            return "typing"
        return None

    async def _generate_for_chat(self, client, chat_id: str) -> bool:
        """Send the accumulated batch to the pipeline.

        Returns True if a new message arrived mid-generation OR the peer
        started typing (caller restarts: re-debounce + wait for message).
        """
        event = self._events[chat_id]

        # If interrupted before we even drained the batch — keep pending
        # intact so nothing is lost on restart.
        pre_reason = self._interrupted(chat_id, event)
        if pre_reason == "typing":
            print("[Batch] Peer typing before generation — waiting for message")
            return True
        if pre_reason == "message":
            # New message arrived after debounce — re-debounce to include it.
            return True

        pending = self._pending.get(chat_id, [])
        self._pending[chat_id] = []
        batch_text = "\n".join(pending) if pending else ""
        if not batch_text.strip():
            return False

        bot = self.bot
        user_id = bot.pipeline.user_id

        # Compress context before processing (oldest messages → summary).
        await bot._compress_context(chat_id)

        # ── Needs pre-filter (once per batch) ──
        needs = get_needs_tracker().get_needs()
        hour = needs["current_hour"]
        is_night = hour >= 22 or hour <= 7
        if needs["fatigue"] > 0.85 and is_night:
            print(f"[Needs] Fatigue {needs['fatigue']:.2f} at night — auto silence")
            add_chunk(
                chat_id, str(bot.me.id),
                content="",
                llm_full_response="[auto-silence: высокая усталость]",
                token_count=0,
                type='raw'
            )
            get_needs_tracker().apply_action_effect("silence")
            return False

        # ── Typing indicator — runs only while the pipeline is processing ──
        stop_typing = asyncio.Event()
        async def typing_loop():
            chat_int = int(chat_id)
            while not stop_typing.is_set():
                try:
                    await send_typing(client, chat_int)
                except Exception:
                    pass
                await asyncio.sleep(4)
        typing_task = asyncio.create_task(typing_loop())

        try:
            # Build subject histories from the context buffer.
            raw_user_visible, raw_internal = bot._build_histories(chat_id)
            user_visible_history = bot._resolve_roles_in_histories(raw_user_visible, chat_id)
            agent_internal_history = bot._resolve_roles_in_histories(raw_internal, chat_id)

            # Pin the pipeline to this chat/user.
            bot.pipeline.user_id = user_id
            bot.pipeline.chat_id = chat_id

            full_response = ""
            has_silence_action = False
            sent_any = False

            async for gen_event in bot.pipeline.process_message_stream(
                user_message=batch_text,
                user_visible_history=user_visible_history,
                agent_internal_history=agent_internal_history,
                user_profile=get_user_info(user_id)
            ):
                # If a new message arrived or the peer started typing
                # mid-generation → stop and wait for their message.
                reason = self._interrupted(chat_id, event)
                if reason == "message":
                    print("[Batch] New message during generation — cancelling run")
                    return True
                if reason == "typing":
                    print("[Batch] Peer typing during generation — cancelling run")
                    return True

                etype = gen_event.get("type")
                if etype == "thought":
                    preview = gen_event.get("thoughts", "")[:120].replace("\n", " ")
                    print(f"  Thoughts: {preview}...")
                elif etype == "action":
                    action = gen_event.get("action", {})
                    if action.get("type") == "send_message":
                        # Last-chance check right before sending: don't
                        # interrupt the user while they type.
                        pre_send = self._interrupted(chat_id, event)
                        if pre_send == "message":
                            print("[Batch] New message before send — cancelling run")
                            return True
                        if pre_send == "typing":
                            print("[Batch] Peer typing before send — cancelling run")
                            return True
                        send_text_content = action.get("text", "").strip()
                        target_chat_id = action.get("chat_id", chat_id)
                        if send_text_content:
                            print(f"\n[SENDING] {send_text_content[:100]}...")
                            await asyncio.sleep(random.uniform(0.3, 1.0))
                            try:
                                await send_text(
                                    client,
                                    int(target_chat_id),
                                    send_text_content,
                                )
                                add_chunk(
                                    target_chat_id, str(bot.me.id),
                                    content=send_text_content,
                                    llm_full_response=full_response or send_text_content,
                                    token_count=0,
                                    type='raw'
                                )
                                push_message(target_chat_id, send_text_content, source_chunk_id=None, role="assistant")
                                sent_any = True
                                print("[✓] Message sent to Telegram")
                            except Exception as e:
                                print(f"[❌ ERROR] Failed to send: {e}")
                        else:
                            print("\n[Silent Mode] AI thought but chose not to respond")
                    elif action.get("type") == "silence":
                        has_silence_action = True
                        print(f"\n[Silence Mode] No response. Reason: {action.get('reason', '')}")
                    elif action.get("type") == "search_memory":
                        print(f"[Memory] Search: '{action.get('query', '')}'")
                    elif action.get("type") == "write_diary":
                        print(f"\n[Diary] Entry written")
                    elif action.get("type") == "update_diary":
                        print(f"\n[Diary] Entry updated")
                elif etype == "search_result":
                    print("  [Search results added to context]")
                elif etype == "done":
                    result = gen_event.get("result", {})
                    full_response = result.get("thoughts", full_response)
                elif etype == "error":
                    print(f"\n[Pipeline Error] {gen_event.get('error')}")

            # ── Post-processing (fallback send / silent save) ──
            # Don't fallback-send while the user is typing or a new message
            # arrived — restart and wait for the full message instead.
            post_reason = self._interrupted(chat_id, event)
            if post_reason is not None:
                print(f"[Batch] Interrupted after generation ({post_reason}) — waiting for message")
                return True
            if not sent_any and not has_silence_action and full_response:
                stripped = full_response.strip()
                if len(stripped) > 80:
                    print("\n[⚠️ FALLBACK] No send_message — auto-sending...")
                    await asyncio.sleep(random.uniform(0.5, 2.0))
                    try:
                        await send_text(client, int(chat_id), stripped)
                        add_chunk(chat_id, str(bot.me.id), content=stripped,
                                  llm_full_response=stripped, token_count=0, type='raw')
                        push_message(chat_id, stripped, source_chunk_id=None, role="assistant")
                        print("[✓] Fallback sent")
                    except Exception as e:
                        print(f"[✗ ERROR] Fallback send failed: {e}")
                elif full_response:
                    add_chunk(chat_id, str(bot.me.id), content="",
                              llm_full_response=full_response, token_count=0, type='raw')
            elif has_silence_action and full_response:
                add_chunk(chat_id, str(bot.me.id), content="",
                          llm_full_response=full_response, token_count=0, type='raw')

            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            print("[❌ CRITICAL ERROR] Unhandled batch exception:")
            traceback.print_exc()
            return False
        finally:
            stop_typing.set()
            typing_task.cancel()

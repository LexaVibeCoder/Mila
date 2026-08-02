# core/queue.py
"""
Per-chat message batching with debounce.

Fixures the multi-message race: instead of spawning a pipeline run per incoming
message, each chat gets a single worker that:

1. Collects messages pushed to the context buffer during a quiet window.
2. Restarts the window whenever a new message arrives (debounce).
3. Before generating, waits briefly while the user keeps typing (best-effort).
4. Sends the accumulated messages to the LLM once.
5. If a new message arrives DURING generation — cancels the LLM run and
   restarts the whole cycle with the new message included.
"""

import asyncio
import random

from pyrogram import enums
from pyrogram.types import Message

from config import settings
from core.db import (
    ensure_user_exists, ensure_chat_exists, add_chunk, get_user_info
)
from core.context.buffer import push_message
from core.needs.tracker import get_tracker as get_needs_tracker


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

    # ------------------------------------------------------------------
    # Public entry point, called from the message handler
    # ------------------------------------------------------------------

    async def handle(self, client, message: Message):
        user_id = str(message.from_user.id)
        chat_id = str(message.chat.id)
        text = message.text

        # Always save to canonical storage immediately.
        ensure_user_exists(user_id, message.from_user.first_name or "Unknown")
        ensure_chat_exists(chat_id, message.chat.first_name or "Личный чат")
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

                # 2. Wait a bit longer while the user is still typing.
                await self._wait_typing(chat_id, client)

                # 3. Generate for whatever accumulated; restart if new message
                #    arrived while we were generating.
                restart = await self._generate_for_chat(client, chat_id)
                if restart:
                    continue

                # No new messages and generation finished — session is done.
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
        Best-effort extra wait while the user keeps typing.

        Telegram/Pyrogram do not expose the peer's typing flag via the public
        API, so we approximate with a bounded wait that stops early as soon as a
        new message actually arrives. Swap this body out if a raw-update typing
        source becomes available.
        """
        event = self._events[chat_id]
        waited = 0.0
        poll_step = 0.5
        while waited < settings.TYPING_WAIT_MAX_SECONDS:
            if event.is_set():
                return  # a message arrived — the debounce/worker handled it
            await asyncio.sleep(poll_step)
            waited += poll_step

    async def _generate_for_chat(self, client, chat_id: str) -> bool:
        """Send the accumulated batch to the pipeline.

        Returns True if a new message arrived mid-generation (caller restarts).
        """
        event = self._events[chat_id]
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
            while not stop_typing.is_set():
                try:
                    await client.send_chat_action(chat_id, enums.ChatAction.TYPING)
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
                # If a new message arrived mid-generation → cancel & restart.
                if event.is_set():
                    print("[Batch] New message during generation — cancelling run")
                    return True

                etype = gen_event.get("type")
                if etype == "thought":
                    preview = gen_event.get("thoughts", "")[:120].replace("\n", " ")
                    print(f"  Thoughts: {preview}...")
                elif etype == "action":
                    action = gen_event.get("action", {})
                    if action.get("type") == "send_message":
                        send_text = action.get("text", "").strip()
                        target_chat_id = action.get("chat_id", chat_id)
                        if send_text:
                            print(f"\n[SENDING] {send_text[:100]}...")
                            await asyncio.sleep(random.uniform(0.3, 1.0))
                            try:
                                await client.send_message(int(target_chat_id), send_text)
                                add_chunk(
                                    target_chat_id, str(bot.me.id),
                                    content=send_text,
                                    llm_full_response=full_response or send_text,
                                    token_count=0,
                                    type='raw'
                                )
                                push_message(target_chat_id, send_text, source_chunk_id=None)
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
                elif etype == "search_result":
                    print("  [Search results added to context]")
                elif etype == "done":
                    result = gen_event.get("result", {})
                    full_response = result.get("thoughts", full_response)
                elif etype == "error":
                    print(f"\n[Pipeline Error] {gen_event.get('error')}")

            # ── Post-processing (fallback send / silent save) ──
            if not sent_any and not has_silence_action and full_response:
                stripped = full_response.strip()
                if len(stripped) > 80:
                    print("\n[⚠️ FALLBACK] No send_message — auto-sending...")
                    await asyncio.sleep(random.uniform(0.5, 2.0))
                    try:
                        await client.send_message(int(chat_id), stripped)
                        add_chunk(chat_id, str(bot.me.id), content=stripped,
                                  llm_full_response=stripped, token_count=0, type='raw')
                        push_message(chat_id, stripped, source_chunk_id=None)
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
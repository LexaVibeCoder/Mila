"""
Per-chat notification worker with wake-on-every-event + peer typing awareness.

v1 presence model: every incoming message is a NOTIFICATION, not an order.
Each notification wakes the LLM once (no debounce — Mila herself decides:
answer, switch state, or stay silent). Typing-wait is kept so we don't
interrupt a human mid-typing.
"""

import asyncio
import random
import re
import time

from pytdbot import types

from config import settings
from core.db import (
    ensure_user_exists, ensure_chat_exists, add_chunk, get_user_info,
    get_user_display_name, log_media,
)
from core.context.buffer import push_message
from core.needs.tracker import get_tracker as get_needs_tracker
from core.tg_adapter import (
    normalize_event, download_notification_media,
    send_text, send_typing,
    parse_chat_action_update, is_typing_like_action, is_cancel_action,
)


# Model sometimes copies the "[YYYY-MM-DD HH:MM:SS]" metadata prefix from
# history context into its reply. That prefix is system metadata, never speech.
_LEADING_TIMESTAMP_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\]\s*"
)


def strip_leading_timestamp(text: str) -> str:
    """Remove a leading '[date time]' copied from history context."""
    return _LEADING_TIMESTAMP_RE.sub("", text or "", count=1)


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
        self._chat_user = {}   # chat_id -> sender user_id (for persona/pipeline)
        self._batch_media = {}  # chat_id -> {"image_data_urls": [...], "audio": (b64, fmt)|None}
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
        notif = await normalize_event(client, message)
        user_id = notif.user_id
        chat_id = notif.chat_id
        placeholder = notif.placeholder_text()
        if not placeholder.strip():
            placeholder = f"[{notif.kind}]"

        # Always save to canonical storage immediately.
        ensure_user_exists(user_id, notif.sender_name)
        ensure_chat_exists(chat_id, notif.chat_name)

        # Download photo/voice for native LLM input (best-effort).
        local_path = None
        if notif.kind in ("photo", "voice"):
            try:
                local_path = await download_notification_media(
                    client, notif, max_mb=getattr(settings, "MAX_MEDIA_MB", 20))
            except Exception as e:
                print(f"[Media] download skipped: {e}")
            if local_path:
                notif.local_path = local_path
                try:
                    log_media(chat_id, user_id, local_path, placeholder[:200])
                except Exception:
                    pass

        # If Mila is sleeping — just queue the message, no batching.
        from core.sleep.manager import is_sleeping, queue_incoming_message
        if is_sleeping():
            queue_incoming_message(chat_id, user_id, placeholder)
            print(f"[Sleep] Message queued from {user_id[:8]}... in {chat_id[:8]}...")
            return

        chunk_id = add_chunk(chat_id, user_id, placeholder, token_count=0,
                             type='raw', kind=notif.kind, media_path=local_path)

        # Push to working memory now so it appears in the pending batch.
        push_message(chat_id, placeholder, source_chunk_id=chunk_id,
                     kind=notif.kind)

        # Accumulate text for this batch.
        self._pending.setdefault(chat_id, []).append(placeholder)
        self._chat_user[chat_id] = user_id

        # Accumulate native media for the current batch.
        if local_path:
            try:
                from core.media import encode_image_for_llm, encode_audio_for_llm
                slot = self._batch_media.setdefault(
                    chat_id, {"image_data_urls": [], "audio": None})
                if notif.kind == "photo":
                    url = encode_image_for_llm(local_path)
                    if url:
                        slot["image_data_urls"].append(url)
                elif notif.kind == "voice":
                    audio = encode_audio_for_llm(local_path)
                    if audio:
                        slot["audio"] = audio
            except Exception as e:
                print(f"[Media] encode skipped: {e}")

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
                # 1. Wake-on-every-notification (v1): no debounce — Mila herself
                #    decides on each event. Just yield once so a photo+caption
                #    arriving in the same tick coalesce.
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
        """Wake-on-every-notification: return on next tick (no quiet window).

        Typing-wait below still protects against interrupting the human.
        MESSAGE_DEBOUNCE_SECONDS is intentionally ignored in v1 presence model.
        """
        event = self._events[chat_id]
        event.clear()
        await asyncio.sleep(0)
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

    def _restore_pending(self, chat_id: str, batch_text: str) -> None:
        """Put a drained-but-undelivered batch back to the front of the queue.

        Called when a run is aborted (new message / peer typing) after its
        batch was already drained: without this the worker would restart
        with an empty queue and the turn would be silently dropped.
        Restarting is cheap — pre-drain checks return before any LLM call.
        """
        if not (batch_text or "").strip():
            return
        current = self._pending.get(chat_id) or []
        self._pending[chat_id] = [batch_text] + current

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

        media = self._batch_media.pop(chat_id, None)
        if media and not media.get("image_data_urls") and media.get("audio") is None:
            media = None

        bot = self.bot
        # Actual sender (fixes old bug where pipeline.user_id stayed "system",
        # so Mila never saw the user's name).
        user_id = self._chat_user.get(chat_id, bot.pipeline.user_id)

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

        # NOTE: no typing indicator during the decision phase on purpose.
        # The notification is not an order — Mila may stay silent or switch
        # state. "Typing..." is shown only right before an actual send,
        # so Telegram never shows her typing while she is merely deciding.
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
                user_profile=get_user_info(user_id),
                media=media,
            ):
                # If a new message arrived or the peer started typing
                # mid-generation → stop and wait for their message.
                # The batch was already drained: put it back if nothing was
                # delivered yet, otherwise this turn would be silently lost.
                reason = self._interrupted(chat_id, event)
                if reason is not None:
                    if not sent_any and not has_silence_action:
                        self._restore_pending(chat_id, batch_text)
                    print(f"[Batch] Interrupted during generation ({reason}) — batch restored")
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
                        if pre_send is not None:
                            if not sent_any and not has_silence_action:
                                self._restore_pending(chat_id, batch_text)
                            print(f"[Batch] Interrupted before send ({pre_send}) — batch restored")
                            return True
                        send_text_content = strip_leading_timestamp(
                            action.get("text", "").strip()
                        )
                        target_chat_id = action.get("chat_id", chat_id)
                        if send_text_content:
                            print(f"\n[SENDING] {send_text_content[:100]}...")
                            try:
                                await send_typing(client, int(target_chat_id))
                            except Exception:
                                pass
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
                    elif action.get("type") in ("enter_dialog", "enter_rest", "enter_working"):
                        print(f"\n[Presence] {action.get('type')} → {action.get('chat_id', action.get('state', ''))}")
                    elif action.get("type") == "start_sleep":
                        has_silence_action = True
                        print(f"\n[Sleep] Started for {action.get('duration_minutes')} min")
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

            # ── Post-processing (silent save) ──
            # NOTE: there is intentionally NO fallback send of `thoughts`:
            # thoughts are the private monologue and must never reach the chat.
            # If the model wants the user to see text, it must use send_message.
            # Don't do anything while the user is typing or a new message
            # arrived — restore the batch and wait for the full message instead.
            post_reason = self._interrupted(chat_id, event)
            if post_reason is not None:
                if not sent_any and not has_silence_action:
                    self._restore_pending(chat_id, batch_text)
                print(f"[Batch] Interrupted after generation ({post_reason}) — batch restored")
                return True
            if not sent_any and not has_silence_action and full_response:
                print("[Silent] No send_message action — staying silent (thoughts are private)")
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

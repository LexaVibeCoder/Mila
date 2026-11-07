"""
Direct-tool reasoning pipeline.

Scheme:
  rest/working --notification(last + 5-10 prev)--> enter_dialog -->
  dialog --[search_memory/diary directly]--> plain text reply.

- tool_choice is always "auto": the model herself decides when a tool
  is needed.
- dialog replies are plain text; bubbles are separated by blank lines.
  An empty reply means no answer.
- rest/working replies only via enter_dialog (full) or send_message
  (one short note); empty reply with no tools means no answer.

Each turn produces a detailed JSON log saved to LOG_DIR.
"""

import json
import os
import re
import time
import uuid
import asyncio
from typing import List, Dict, Any, Optional, AsyncGenerator
from datetime import datetime

from config import settings
from core.action_journal import get_journal_context
from core.delivery import TurnContext
from core.llm.client import LLMClient
from core.tools.executor import ToolExecutor, get_tool_definitions
from core.llm.persona import build_main_llm_system_prompt
from core.llm.embeddings import EmbeddingClient
from core.memory.vector import VectorMemory
from core.needs.tracker import get_tracker as get_needs_tracker


# Try to import Observatory modules for dynamic config
try:
    from Mila_observatory.llm_manager import llm_manager
    from Mila_observatory.runtime import runtime_config
    HAS_OBSERVATORY = True
except ImportError:
    HAS_OBSERVATORY = False

MAX_THINK_ITERATIONS = 7
# Keep old runtime key name for compat (Observatory panel knob).
MAX_TOOL_ITERATIONS = MAX_THINK_ITERATIONS


def _max_iterations() -> int:
    """Tool-loop cap: Observatory knob wins, builtin default otherwise."""
    if HAS_OBSERVATORY:
        try:
            value = runtime_config.get(
                "pipeline.max_think_iterations", MAX_THINK_ITERATIONS
            )
            if isinstance(value, int) and value > 0:
                return value
        except Exception:
            pass
    return MAX_THINK_ITERATIONS


def _tool_choice() -> str:
    """Always auto for the main pipeline.

    The old default was "required" for the Think wrapper. With direct tools
    + content replies, "required" would force a tool call every turn and the
    model could never just reply or stay silent. Night/sleep workers keep
    their own tool_choice.
    """
    if HAS_OBSERVATORY:
        try:
            v = runtime_config.get("llm.tool_choice", "auto")
            if v == "required":
                print("  [Pipeline] runtime llm.tool_choice=required overridden to auto (direct-tool scheme)")
                return "auto"
            if isinstance(v, str) and v:
                return v
        except Exception:
            pass
    return "auto"


def _get_presence_state() -> dict:
    try:
        from core.presence import get_presence
        return get_presence()
    except Exception:
        return {"state": "rest", "focus_chat_id": None,
                "focus_user_name": None, "note": "", "updated_at": None}


# Reminder when rest/working returns text without entering dialog.
REST_CONTENT_REMINDER = (
    "PROTOCOL: you are in rest/working. Text replies from here are IGNORED. "
    "To talk, call enter_dialog with the notification chat_alias first "
    "(you will be called again in dialog, then answer there). "
    "For a one-line note use the send_message tool once. "
    "To skip, return an empty reply with no tool calls."
)

DIALOG_REPLY_REMINDER = (
    "PROTOCOL: in dialog reply with plain text only. "
    "Separate message bubbles with a blank line. "
    "An empty reply means no answer."
)


# "[YYYY-MM-DD HH:MM(:SS)]" copied from history metadata by the model.
_TIMESTAMP_RE = r"\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\]"
_LEADING_TIMESTAMP_RE = re.compile(rf"^{_TIMESTAMP_RE}\s*")
# Bubble boundary: a blank line, or a newline followed by a line-leading
# timestamp. The model sometimes marks a wanted split with its own timestamp
# instead of a blank line.
_BUBBLE_BREAK_RE = re.compile(rf"\n\s*\n+|\n(?=\s*{_TIMESTAMP_RE})")


def _strip_leading_timestamp(text: str) -> str:
    return _LEADING_TIMESTAMP_RE.sub("", text or "", count=1)


def split_reply_bubbles(raw: str) -> list[str]:
    """Split a plain-text dialog reply into bubbles.

    Boundaries are blank lines and a newline followed by a line-leading
    "[date time]" marker: the model sometimes uses its own timestamp to mark
    where a new bubble should start. Single newlines stay inside a bubble.
    An empty list means no answer.
    """
    if not raw:
        return []
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    bubbles: list[str] = []
    for part in _BUBBLE_BREAK_RE.split(text):
        cleaned = _strip_leading_timestamp(part.strip())
        if cleaned:
            bubbles.append(cleaned)
    return bubbles


class ThinkPipeline:
    """Direct-tool pipeline (kept name for backward compat with bot.py).

    Alias: MilaPipeline.
    """

    def __init__(
        self,
        text_memory: VectorMemory,
        embedding_client: EmbeddingClient,
        user_id: str,
        chat_id: str,
        my_user_id: str = None,
    ):
        self.text_memory = text_memory
        self.emb_client = embedding_client
        self.user_id = user_id
        self.chat_id = chat_id
        self.my_user_id = my_user_id

        if not HAS_OBSERVATORY:
            self._fallback_llm = LLMClient()

    @property
    def main_llm(self) -> LLMClient:
        if HAS_OBSERVATORY:
            return llm_manager.get_main_client()
        return self._fallback_llm

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_tool_defs(self, include_proactive: bool = False,
                           state: str | None = None) -> List[Dict]:
        # `state` comes from the turn snapshot. Re-reading global presence here
        # would let a parallel chat's enter_dialog change this turn's tools.
        if state is None:
            state = _get_presence_state().get("state", "rest")
        try:
            return get_tool_definitions(presence_state=state,
                                        include_proactive=include_proactive)
        except TypeError:
            # Old signature fallback (should not happen after migration).
            return get_tool_definitions()

    def _build_notification_block(self, batch_text: str, chat_id: str | None = None,
                                  presence: dict | None = None) -> Optional[str]:
        """Explicit notification wrapper for rest/working.

        History (agent_internal_history) already carries 5-10+ previous
        messages within the token limit; this block marks which part is the
        NEW notification vs context so the model in rest can decide whether
        enter_dialog is worth it.

        `presence` is the turn snapshot; without it the current global state
        is read (legacy callers/tests).
        """
        presence = presence if presence is not None else _get_presence_state()
        state = presence.get("state", "rest")
        if state not in ("rest", "working"):
            return None
        last = (batch_text or "").strip()[:2000]
        if not last:
            return None
        from core.db import get_chat_alias
        alias = get_chat_alias(chat_id or self.chat_id) or "неизвестный чат"
        return (
            f"[уведомление] новый текст из чата {alias}:\n"
            f"{last}\n"
            f"(выше в истории — предыдущие сообщения этого чата для контекста.)"
        )

    @staticmethod
    def _tool_calls_to_actions(tool_calls) -> List[Dict]:
        """Convert OpenAI tool_calls to executor action dicts.

        Returns [{"action_type": name, "params": {...}, "_call_id": id}, ...].
        Malformed JSON args are skipped (logged).
        """
        actions: List[Dict] = []
        for tc in tool_calls or []:
            try:
                name = tc.function.name
                raw = tc.function.arguments or "{}"
                try:
                    params = json.loads(raw) if raw.strip() else {}
                except (json.JSONDecodeError, AttributeError):
                    print(f"[Pipeline] bad tool args for {name}: {raw[:200]}")
                    continue
                if not isinstance(params, dict):
                    params = {}
                actions.append({
                    "action_type": name,
                    "params": params,
                    "_call_id": getattr(tc, "id", None),
                })
            except Exception as e:
                print(f"[Pipeline] tool_call convert error: {e}")
        return actions

    async def _execute_single_action(self, action: Dict, user_id: str | None = None, chat_id: str | None = None,
                                     seen_memories: set[str] | None = None) -> Dict:
        """Execute a single direct-tool action with an explicit request context.

        user_id/chat_id are snapshotted per-turn by the caller so two parallel
        chats can't overwrite each other's Tools via self.user_id mutation.
        seen_memories is shared by all searches within the turn to keep
        duplicate facts out of the context.
        """
        executor = ToolExecutor(
            text_memory=self.text_memory,
            embedding_client=self.emb_client,
            current_user_id=user_id or self.user_id,
            chat_id=chat_id or self.chat_id,
            seen_memories=seen_memories,
        )
        return await executor.execute_action(action)

    @staticmethod
    def _attach_media(messages: List[Dict], media: Dict[str, Any],
                      default_chat_id: str = "") -> List[Dict]:
        """Attach native photo/audio to history + current event.

        - Current batch bytes go to the LAST user message (as before).
        - Recent history bytes (last 3 photos + last 2 audios from
          chunks.media_path) are re-attached ONLY to their own user
          messages (matched by placeholder). If the native message is
          no longer in context (summarized away), its bytes are dropped —
          never folded into the current message as fresh info.
        Falls back to text-only if nothing usable.
        """
        try:
            from core.media import (
                build_multimodal_parts,
                encode_image_for_llm,
                encode_audio_for_llm,
                MAX_HISTORY_IMAGES,
                MAX_HISTORY_AUDIOS,
            )
            media = media or {}
            image_urls = list(media.get("image_data_urls") or [])
            audios: list = list(media.get("audios") or [])
            if media.get("audio"):
                audios.append(media.get("audio"))
            event_line = media.get("event_line", "")
            chat_id = str(media.get("chat_id") or default_chat_id or "")

            current_image_paths = list(media.get("image_paths") or [])
            current_audio_paths = list(media.get("audio_paths") or [])

            # --- History re-attach -------------------------------------
            hist_image_urls: list[tuple] = []  # (url, path)
            hist_audios: list[tuple] = []  # ((b64,fmt), path)
            if chat_id:
                try:
                    from core.db import get_recent_media
                    recent = get_recent_media(
                        chat_id, kinds=("photo", "voice"),
                        limit=MAX_HISTORY_IMAGES + MAX_HISTORY_AUDIOS)
                    cur_img = set(current_image_paths)
                    cur_aud = set(current_audio_paths)
                    need_img = max(0, MAX_HISTORY_IMAGES - len(image_urls))
                    need_aud = max(0, MAX_HISTORY_AUDIOS - len(audios))
                    for r in reversed(recent):
                        kind = r.get("kind")
                        mp = r.get("media_path") or ""
                        if not mp or mp in cur_img or mp in cur_aud:
                            continue
                        if kind == "photo" and need_img > 0:
                            url = encode_image_for_llm(mp)
                            if url:
                                hist_image_urls.append((url, mp))
                                need_img -= 1
                        elif kind == "voice" and need_aud > 0:
                            au = encode_audio_for_llm(mp)
                            if au:
                                hist_audios.append((au, mp))
                                need_aud -= 1
                        if need_img <= 0 and need_aud <= 0:
                            break
                except Exception as e:
                    print(f"[Pipeline] history media lookup failed: {e}")

            if not image_urls and not audios and not hist_image_urls and not hist_audios:
                if event_line:
                    messages = list(messages)
                    messages.append({"role": "system", "content": event_line})
                return messages

            messages = list(messages)

            # Attach history media to their own messages: walk from the end,
            # find text-only user messages with matching placeholders.
            # Last message is reserved for current-batch media.
            hist_img_rev = list(reversed(hist_image_urls))
            hist_aud_rev = list(reversed(hist_audios))
            last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    last_user_idx = i
                    break
            for i in range(len(messages) - 1, -1, -1):
                if not hist_img_rev and not hist_aud_rev:
                    break
                if i == last_user_idx:
                    continue
                m = messages[i]
                if m.get("role") != "user":
                    continue
                base = m.get("content", "")
                if isinstance(base, list):
                    continue
                txt = str(base)
                want_img = "[фото" in txt and hist_img_rev
                want_aud = ("[голосовое" in txt or "[аудио" in txt) and hist_aud_rev
                if not want_img and not want_aud:
                    continue
                imgs: list = []
                auds: list = []
                if want_img:
                    url, _p = hist_img_rev.pop(0)
                    imgs.append(url)
                if want_aud:
                    (au, _p) = hist_aud_rev.pop(0)
                    auds.append(au)
                parts = build_multimodal_parts(txt, imgs or None, auds or None)
                messages[i] = {"role": "user", "content": parts}

            # History media whose placeholder wasn't found (summarized away):
            # drop the bytes — they stay in history as plain-text placeholders
            # only. Folding them into the last user message would present
            # stale photos/audios as fresh info for the current turn.
            if hist_img_rev or hist_aud_rev:
                print(f"[Pipeline] history media without native message dropped: "
                      f"{len(hist_img_rev)} img + {len(hist_aud_rev)} audio")

            # Enrich last user message with current-batch media
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    base = messages[i].get("content", "")
                    if isinstance(base, list):
                        try:
                            for url in image_urls:
                                base.append({"type": "image_url",
                                             "image_url": {"url": url}})
                            for b64, fmt in audios:
                                base.append({"type": "input_audio",
                                             "input_audio": {"data": b64, "format": fmt}})
                            messages[i] = {"role": "user", "content": base}
                        except Exception:
                            pass
                        break
                    parts = build_multimodal_parts(str(base), image_urls or None,
                                                   audios or None)
                    messages[i] = {"role": "user", "content": parts}
                    break
            if event_line:
                messages.append({"role": "system", "content": event_line})
            n_dropped_img = len(hist_img_rev)
            n_dropped_aud = len(hist_aud_rev)
            n_img = len(image_urls) + len(hist_image_urls) - n_dropped_img
            n_aud = len(audios) + len(hist_audios) - n_dropped_aud
            if n_img or n_aud or n_dropped_img or n_dropped_aud:
                print(f"[Pipeline] media attached: {n_img} img + {n_aud} audio "
                      f"(current {len(image_urls)}+{len(audios)}, history "
                      f"{len(hist_image_urls) - n_dropped_img}+{len(hist_audios) - n_dropped_aud}, "
                      f"dropped {n_dropped_img}+{n_dropped_aud})")
            return messages
        except Exception as e:
            print(f"[Pipeline] media attach failed, text-only: {e}")
            return messages

    # ------------------------------------------------------------------
    # Core loop (shared by stream / non-stream)
    # ------------------------------------------------------------------

    async def _run_stream(
        self,
        user_message: str,
        agent_internal_history: List[Dict[str, str]],
        media: Optional[Dict[str, Any]],
        request_user_id: str | None = None,
        request_chat_id: str | None = None,
        turn: TurnContext | None = None,
        enable_peek: bool = False,
        proactive_block: str | None = None,
    ):
        """Yield low-level step dicts; caller translates to public events.

        request_user_id/request_chat_id snapshot the turn: parallel chats
        mutate self.user_id/self.chat_id (queue pinning), so the loop must
        never read self.* mid-flight — only these locals.

        `turn` owns delivery: the reply steps deliberately carry no chat id,
        so the caller (queue/proactive) can only send to the turn owner or an
        explicitly granted target. Presence is snapshotted once here; a
        parallel turn switching focus cannot change this turn's mode, prompt
        or tools mid-flight. Only a switch performed by THIS turn refreshes it.

        Yields internal dicts:
          {"kind": "llm", "content": str, "tool_calls": [...], "finish_reason": str}
          {"kind": "action", "action": executed_dict}
          {"kind": "reply_bubbles", "texts": [...]}
          {"kind": "reply_tool", "action": executed send_message dict}
          {"kind": "silence", "reason": str}
        """
        req_user = request_user_id or self.user_id
        req_chat = request_chat_id or self.chat_id
        # Snapshot presence once for the whole turn (see docstring).
        presence = (
            dict(turn.presence) if turn is not None
            else _get_presence_state()
        )
        state = presence.get("state", "rest")
        # Snapshot the journal once for the whole turn. Events created by this
        # turn are appended at the bottom as they happen; they enter the
        # journal block only on the next turn.
        try:
            journal_context = get_journal_context()
        except Exception as e:
            print(f"[Pipeline] journal read failed: {e}")
            journal_context = ""
        # A proactive turn has no owner chat yet: its target is the dialog it
        # opens, or the dialog that is already open at turn start.
        if turn is not None and turn.proactive and state == "dialog":
            turn.allow(presence.get("focus_chat_id"))
        messages = list(agent_internal_history)
        if proactive_block:
            # Proactive turn: instructions + chat list on top, no fake notification.
            messages.append({"role": "system", "content": proactive_block})
        else:
            notif_block = self._build_notification_block(user_message or "", chat_id=req_chat,
                                                         presence=presence)
            if notif_block:
                messages.append({"role": "system", "content": notif_block})
        # Always attach: history media (last 3 photos + 2 audios) must load
        # even on text-only turns (media=None). chat_id fallback = req_chat.
        messages = self._attach_media(messages, media or {}, default_chat_id=req_chat)

        max_iterations = _max_iterations()
        choice = _tool_choice()
        iteration = 0
        # Facts already injected by search_memory during this turn. Shared by
        # every search so overlapping queries can't duplicate the same fact.
        seen_memories: set[str] = set()

        while iteration < max_iterations:
            iteration += 1
            # `presence`/`state` are the turn snapshot, refreshed below only
            # when THIS turn switches state itself. Parallel turns must not
            # bleed their focus into this prompt/tools.
            print(f"\n── Iteration {iteration}/{max_iterations} [{state}] ──")

            main_system_prompt = build_main_llm_system_prompt(
                req_user, chat_id=req_chat, my_user_id=self.my_user_id,
                presence=presence, journal_context=journal_context,
            )
            # Peek is proactive-only: keep reactive turns clean.
            # After enter_dialog the turn becomes dialog and peek drops out.
            tool_definitions = self._current_tool_defs(
                include_proactive=bool(enable_peek and proactive_block),
                state=state)

            response = await self.main_llm.generate(
                messages=messages,
                system_prompt=main_system_prompt,
                tools=tool_definitions if tool_definitions else None,
                tool_choice=choice if tool_definitions else "none",
                max_tokens=2048,
                temperature=self.main_llm.default_temperature,
            )

            content = (response.get("thoughts", "") or "")
            tool_calls = response.get("tool_calls", None)

            preview = content[:120].replace("\n", " ")
            print(f"  Content: {preview}..." if preview else "  Content: <empty>")
            if tool_calls:
                for tc in tool_calls:
                    print(f"  → {tc.function.name}()")

            yield {
                "kind": "llm",
                "iteration": iteration,
                "content": content,
                "reasoning": response.get("reasoning", ""),
                "tool_calls": [
                    {"name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (tool_calls or [])
                ],
                "finish_reason": response.get("finish_reason"),
                "system_prompt": main_system_prompt,
            }

            actions = self._tool_calls_to_actions(tool_calls)

            # ---- No tools: verdict depends on state ----
            if not actions:
                if state == "dialog":
                    bubbles = split_reply_bubbles(content)
                    if not bubbles:
                        yield {"kind": "silence", "reason": "empty reply"}
                        return
                    # No target here: the recipient is the turn owner (or an
                    # explicit grant), decided by the turn context in routing.
                    yield {"kind": "reply_bubbles", "texts": bubbles}
                    return
                text = content.strip()
                if not text:
                    yield {"kind": "silence", "reason": "empty reply, no tools"}
                    return
                # rest/working + text = protocol violation -> retry
                print("  [Protocol: text reply in rest/working → reminder]")
                messages.append({"role": "system", "content": REST_CONTENT_REMINDER})
                messages.append({"role": "assistant", "content": content})
                if iteration >= max_iterations:
                    yield {"kind": "silence",
                           "reason": "text reply in rest without enter_dialog"}
                    return
                continue

            # ---- Tools present: content this turn is NOT sent ----
            if content.strip():
                messages.append({"role": "assistant", "content": content})

            has_search = False
            has_peek = False
            presence_switched = False
            send_tool_reply = None
            sleep_started = False

            for action in actions:
                executed = await self._execute_single_action(action, user_id=req_user, chat_id=req_chat,
                                                            seen_memories=seen_memories)
                if executed.get("status") != "error":
                    get_needs_tracker().apply_action_effect(action.get("action_type"))
                yield {"kind": "action", "action": executed}

                atype = executed.get("type")
                status = executed.get("status")

                if atype in ("enter_dialog", "send_message") and status == "error":
                    messages.append({"role": "system", "content": (
                        f"{atype} не выполнен: {executed.get('error', 'ошибка')}. "
                        "Выбери известный алиас или промолчи."
                    )})
                    continue

                if atype == "start_sleep" and status == "sleeping":
                    sleep_started = True
                    break

                if atype in ("enter_dialog", "enter_rest", "enter_working") \
                        and status == "switched":
                    presence_switched = True
                    if turn is not None and atype == "enter_dialog":
                        # A proactive turn has no chat of its own: the chat it
                        # just entered is the explicit destination for the
                        # reply that follows in this same turn.
                        if turn.proactive:
                            turn.allow(executed.get("chat_id"))
                    # Update the local snapshot from OUR action only — never
                    # re-read global presence (another turn may be switching).
                    executed_state = executed.get("state")
                    if isinstance(executed_state, dict):
                        # Older executor payloads returned the whole presence
                        # snapshot in "state"; keep the turn snapshot a string.
                        executed_state = executed_state.get("state")
                    if atype == "enter_dialog":
                        presence = {**presence, "state": "dialog",
                                    "focus_chat_id": executed.get("chat_id"),
                                    "focus_user_name": executed.get("user_name")}
                    else:
                        presence = {**presence, "state": executed_state or atype,
                                    "focus_chat_id": None,
                                    "focus_user_name": None}
                    state = presence.get("state", state)
                    # The fresh event goes to the very bottom of the context;
                    # on the next turn it is part of the journal block.
                    event_line = executed.get("journal_event")
                    if event_line:
                        messages.append({"role": "system", "content": (
                            "Presence switched. Decide in the new state now.\n"
                            f"[событие] {event_line}"
                        )})
                    else:
                        from core.db import get_chat_alias
                        target = (get_chat_alias(executed["chat_id"])
                                  if executed.get("chat_id") else atype)
                        messages.append({"role": "system", "content": (
                            f"Presence switched: {atype} → "
                            f"{target}. "
                            f"Decide in the new state now."
                        )})
                    print("  [Presence switched → re-calling]")
                    continue

                if atype == "send_message" and status == "ready":
                    if (executed.get("text") or "").strip():
                        # The model picked the destination explicitly (alias
                        # was resolved by the executor) — allow that target.
                        if turn is not None:
                            turn.allow(executed.get("chat_id"))
                        send_tool_reply = executed
                    continue

                if atype == "search_memory":
                    has_search = True
                    if status == "searched":
                        context_addon = executed.get("context_addon", "[No results found]")
                        # The addon already carries its own header.
                        messages.append({"role": "system", "content": context_addon})
                        print("  [Search results → re-calling]")
                    else:
                        messages.append({"role": "system", "content": (
                            "Memory search failed "
                            f"({executed.get('error', 'unknown error')}). "
                            "Answer from the conversation context or skip."
                        )})
                        print("  [Search error → re-calling anyway]")
                    continue

                if atype in ("write_diary", "update_diary"):
                    if status in ("written", "updated"):
                        messages.append({"role": "system", "content": (
                            f"Diary {atype} done ({status}). Answer now."
                        )})
                    else:
                        messages.append({"role": "system", "content": (
                            f"Diary {atype} failed ({executed.get('error', status)}). "
                            f"Answer from context or skip."
                        )})
                    continue

                if atype == "peek_recent_chats":
                    has_peek = True
                    if status == "peeked":
                        context_addon = executed.get("context_addon", "[нет чатов]")
                        messages.append({"role": "system", "content": (
                            "Recent messages across chats "
                            "(old->new, fresh at the bottom):\n"
                            f"{context_addon}\n"
                            "Теперь выбери ОДИН чат и либо вызови enter_dialog "
                            "с его chat_alias, либо отправь одну записку через "
                            "send_message, либо промолчи пустым ответом."
                        )})
                        print("  [Peek results → re-calling]")
                    else:
                        messages.append({"role": "system", "content": (
                            "Peek failed "
                            f"({executed.get('error', 'unknown error')}). "
                            "Выбери чат из списка или промолчи."
                        )})
                        print("  [Peek error → re-calling anyway]")
                    continue

            if sleep_started:
                yield {"kind": "silence", "reason": "sleep started"}
                return

            if send_tool_reply is not None and not has_search and not has_peek \
                    and not presence_switched:
                # Single note in rest/working — final, no re-call.
                yield {"kind": "reply_tool", "action": send_tool_reply}
                return

            if has_search or has_peek or presence_switched:
                continue

            # Diary-only turn with no verdict yet: re-call.
            if iteration >= max_iterations:
                yield {"kind": "silence", "reason": "tools with no reply"}
                return
            messages.append({"role": "system", "content": (
                "No answer yet. "
                + (DIALOG_REPLY_REMINDER if state == "dialog"
                   else "Answer via tools or skip with an empty reply.")
            )})

        yield {"kind": "silence", "reason": "max iterations reached"}

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def process_message(
        self,
        user_message: str,
        user_visible_history: List[Dict[str, str]],
        agent_internal_history: List[Dict[str, str]],
        user_profile: Optional[Dict[str, Any]] = None,
        media: Optional[Dict[str, Any]] = None,
        request_user_id: str | None = None,
        request_chat_id: str | None = None,
        enable_peek: bool = False,
        proactive_block: str | None = None,
        turn: TurnContext | None = None,
    ) -> Dict[str, Any]:
        total_start = time.time()
        req_user = request_user_id or self.user_id
        req_chat = request_chat_id or self.chat_id
        try:
            all_actions: List[Dict] = []
            raw_llm_responses: List[Dict] = []
            reply_texts: List[str] = []
            reply_text = ""
            reply_via = ""
            reply_chat_id = ""
            silence_reason = ""
            iterations = 0

            async for step in self._run_stream(
                user_message, agent_internal_history, media,
                request_user_id=req_user, request_chat_id=req_chat,
                turn=turn,
                enable_peek=enable_peek, proactive_block=proactive_block,
            ):
                kind = step.get("kind")
                if kind == "llm":
                    iterations = step.get("iteration", iterations)
                    raw_llm_responses.append({
                        "iteration": step.get("iteration"),
                        "thoughts": step.get("content", ""),
                        "reasoning": step.get("reasoning", ""),
                        "tool_calls": step.get("tool_calls", []),
                        "finish_reason": step.get("finish_reason"),
                    })
                elif kind == "action":
                    all_actions.append(step["action"])
                elif kind == "reply_bubbles":
                    reply_texts = list(step["texts"])
                    reply_text = "\n".join(reply_texts)
                    reply_chat_id = (turn.resolve_target() if turn is not None
                                     else req_chat) or ""
                    reply_via = "bubbles"
                    get_needs_tracker().apply_action_effect("send_message")
                    break
                elif kind == "reply_tool":
                    a = step["action"]
                    reply_text = (a.get("text") or "").strip()
                    reply_chat_id = (
                        turn.resolve_target(a.get("chat_id")) if turn is not None
                        else str(a.get("chat_id", req_chat))
                    ) or ""
                    reply_via = "send_message_brush_off"
                    all_actions.append(a) if a not in all_actions else None
                    break
                elif kind == "silence":
                    silence_reason = step.get("reason", "")
                    get_needs_tracker().apply_action_effect("silence")
                    break

            s_time = time.time() - total_start
            # Keep legacy keys (thoughts/actions) for Observatory UI compat.
            result = {
                "success": True,
                "actions": all_actions,
                "thoughts": reply_text,
                "reply_text": reply_text,
                "reply_texts": reply_texts,
                "reply_via": reply_via,
                "reply_chat_id": reply_chat_id,
                "silence_reason": silence_reason,
                "system_prompt": "",
                "llm_messages": [],
                "error": None,
                "raw_llm_responses": raw_llm_responses,
                "timing": {"total": f"{s_time:.2f}s", "iterations": iterations},
            }
            await self._save_log(result, proactive_block or user_message,
                                 request_user_id=req_user, request_chat_id=req_chat)
            return result
        except Exception as e:
            total_time = time.time() - total_start
            import traceback
            tb = traceback.format_exc()
            print(f"[Pipeline Error] {e}\n{tb}")
            error_result = {
                "success": False,
                "actions": [],
                "thoughts": "",
                "reply_text": "",
                "error": str(e),
                "timing": {"total": f"{total_time:.2f}s"},
            }
            await self._save_log({**error_result, "traceback": tb}, user_message, request_user_id=request_user_id, request_chat_id=request_chat_id)
            return error_result

    async def _save_log(self, result: Dict, user_message: str, request_user_id: str | None = None, request_chat_id: str | None = None):
        """Save a detailed JSON log of the pipeline run (rotated, max 200 files)."""
        log_dir = settings.LOG_DIR
        os.makedirs(log_dir, exist_ok=True)
        try:
            # Best-effort rotation: keep newest 200 pipeline_*.json
            import glob
            files = sorted(glob.glob(os.path.join(log_dir, "pipeline_*.json")))
            if len(files) > 200:
                for old in files[:len(files) - 200]:
                    try:
                        os.unlink(old)
                    except OSError:
                        pass
        except Exception:
            pass

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": request_user_id or self.user_id,
            "chat_id": request_chat_id or self.chat_id,
            "user_message": user_message,
            "system_prompt": result.get("system_prompt", ""),
            "llm_messages": result.get("llm_messages", []),
            "thoughts": result.get("thoughts", ""),
            "reply_text": result.get("reply_text", ""),
            "reply_texts": result.get("reply_texts", []),
            "reply_via": result.get("reply_via", ""),
            "reply_chat_id": result.get("reply_chat_id", ""),
            "silence_reason": result.get("silence_reason", ""),
            "actions": result.get("actions", []),
            "raw_llm_responses": result.get("raw_llm_responses", []),
            "success": result.get("success", False),
            "error": result.get("error"),
            "timing": result.get("timing", {}),
            "traceback": result.get("traceback", None),
        }

        filename = f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        filepath = os.path.join(log_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(log_entry, f, ensure_ascii=False, indent=2, default=str)
            print(f"[Log] Saved to {filepath}")
        except Exception as e:
            print(f"[Log Error] Failed to save log: {e}")

    async def process_message_stream(
        self,
        user_message: str,
        user_visible_history: List[Dict[str, str]],
        agent_internal_history: List[Dict[str, str]],
        user_profile: Optional[Dict[str, Any]] = None,
        media: Optional[Dict[str, Any]] = None,
        request_user_id: str | None = None,
        request_chat_id: str | None = None,
        enable_peek: bool = False,
        proactive_block: str | None = None,
        turn: TurnContext | None = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Streaming version consumed by core/queue.py and core/proactive.py.

        Yields:
          {"type": "thought", "thoughts": str}
          {"type": "action", "action": Dict}
          {"type": "search_result", "content": str}
          {"type": "reply", "texts": [...], "via": str}   # no chat id!
          {"type": "silence", "reason": str}
          {"type": "done", "result": Dict}
          {"type": "error", "error": str}

        The reply event intentionally has no destination: the consumer sends
        via the turn context (owner chat or explicit grant).
        """
        total_start = time.time()
        req_user = request_user_id or self.user_id
        req_chat = request_chat_id or self.chat_id
        try:
            all_actions: List[Dict] = []
            raw_llm_responses: List[Dict] = []
            reply_texts: List[str] = []
            reply_text = ""
            reply_via = ""
            reply_chat_id = ""
            silence_reason = ""
            iteration = 0

            async for step in self._run_stream(
                user_message, agent_internal_history, media,
                request_user_id=req_user, request_chat_id=req_chat,
                turn=turn,
                enable_peek=enable_peek, proactive_block=proactive_block,
            ):
                kind = step.get("kind")
                if kind == "llm":
                    iteration = step.get("iteration", iteration)
                    raw_llm_responses.append({
                        "iteration": step.get("iteration"),
                        "thoughts": step.get("content", ""),
                        "reasoning": step.get("reasoning", ""),
                        "tool_calls": step.get("tool_calls", []),
                        "finish_reason": step.get("finish_reason"),
                    })
                    yield {"type": "thought", "thoughts": step.get("content", "")}
                elif kind == "action":
                    action = step["action"]
                    all_actions.append(action)
                    # Needs effect already applied inside _run_stream — do not double-count.
                    yield {"type": "action", "action": action}
                    if action.get("type") == "search_memory" and action.get("status") == "searched":
                        yield {"type": "search_result",
                               "content": action.get("context_addon", "")}
                    if action.get("type") == "peek_recent_chats" and action.get("status") == "peeked":
                        yield {"type": "search_result",
                               "content": action.get("context_addon", "")}
                elif kind == "reply_bubbles":
                    reply_texts = list(step["texts"])
                    reply_text = "\n".join(reply_texts)
                    reply_chat_id = (turn.resolve_target() if turn is not None
                                     else req_chat) or ""
                    reply_via = "bubbles"
                    get_needs_tracker().apply_action_effect("send_message")
                    yield {"type": "reply", "texts": reply_texts, "text": reply_text,
                           "via": "bubbles"}
                    break
                elif kind == "reply_tool":
                    a = step["action"]
                    # _run_stream already applied needs effect; avoid double count
                    # for the action itself (applied inside _run_stream), so just yield.
                    reply_text = (a.get("text") or "").strip()
                    reply_texts = [reply_text] if reply_text else []
                    reply_chat_id = (
                        turn.resolve_target(a.get("chat_id")) if turn is not None
                        else str(a.get("chat_id", req_chat))
                    ) or ""
                    reply_via = "send_message_brush_off"
                    yield {"type": "reply", "texts": reply_texts, "text": reply_text,
                           "via": "send_message_brush_off"}
                    break
                elif kind == "silence":
                    silence_reason = step.get("reason", "")
                    get_needs_tracker().apply_action_effect("silence")
                    yield {"type": "silence", "reason": silence_reason}
                    break

            s_time = time.time() - total_start
            result = {
                "success": True,
                "actions": all_actions,
                "thoughts": reply_text,
                "reply_text": reply_text,
                "reply_texts": reply_texts,
                "reply_via": reply_via,
                "reply_chat_id": reply_chat_id,
                "silence_reason": silence_reason,
                "system_prompt": "",
                "llm_messages": [],
                "error": None,
                "raw_llm_responses": raw_llm_responses,
                "timing": {"total": f"{s_time:.2f}s", "iterations": iteration},
            }
            await self._save_log(result, proactive_block or user_message,
                                 request_user_id=req_user, request_chat_id=req_chat)
            yield {"type": "done", "result": result}
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[Pipeline Error] {e}\n{tb}")
            error_result = {
                "success": False,
                "actions": [],
                "thoughts": "",
                "reply_text": "",
                "error": str(e),
                "timing": {"total": f"{time.time() - total_start:.2f}s"},
            }
            await self._save_log({**error_result, "traceback": tb},
                                 proactive_block or user_message,
                                 request_user_id=request_user_id,
                                 request_chat_id=request_chat_id)
            yield {"type": "error", "error": str(e)}


# Backward/forward-compatible alias.
MilaPipeline = ThinkPipeline

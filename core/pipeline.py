"""
Direct-tool reasoning pipeline.

Scheme:
  rest/working --notification(last + 5-10 prev)--> enter_dialog -->
  dialog --[search_memory/diary directly]--> JSON {"messages": [...]}.

- tool_choice is always "auto": the model herself decides when a tool
  is needed.
- dialog replies are strict JSON with 0-3 message bubbles; empty array
  means no answer. response_format json_schema is requested when the
  provider supports it, otherwise prompt + parser + retry enforce it.
- rest/working replies only via enter_dialog (full) or send_message
  (one short note); empty reply with no tools means no answer.

Each turn produces a detailed JSON log saved to LOG_DIR.
"""

import json
import os
import time
import uuid
import asyncio
from typing import List, Dict, Any, Optional, AsyncGenerator
from datetime import datetime

from config import settings
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
    "To talk, call enter_dialog with the notification chat_id first "
    "(you will be called again in dialog, then answer there). "
    "For a one-line note use the send_message tool once. "
    "To skip, return an empty reply with no tool calls."
)

# Strict reply shape in dialog (also requested via API response_format).
CHAT_MESSAGES_SCHEMA: Dict[str, Any] = {
    "name": "chat_messages",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["messages"],
        "additionalProperties": False,
    },
}

DIALOG_JSON_REMINDER = (
    "PROTOCOL: in dialog return ONLY one JSON object, no text around it: "
    '{"messages": [{"content": "..."}]} (1-3 short bubbles) '
    'or {"messages": []} to skip. No code fences.'
)

MAX_BUBBLES = 3


def _strip_leading_timestamp(text: str) -> str:
    import re
    return re.sub(r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\]\s*", "", text or "", count=1)


def parse_chat_messages(raw: str) -> tuple[list[str] | None, str]:
    """Parse dialog JSON into bubble texts.

    Returns (bubbles, error). bubbles=None means parse/validation failed
    (caller retries). Empty list is valid and means no answer.
    """
    import re as _re
    if raw is None:
        return None, "empty reply"
    text = raw.strip()
    if not text:
        return None, "empty reply"
    # Tolerate code fences and surrounding text: extract first {...} block.
    fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    elif not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"invalid JSON ({e})"
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return None, 'expected {"messages": [...]}'
    bubbles: list[str] = []
    for item in data["messages"][:MAX_BUBBLES]:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        if not isinstance(content, str):
            continue
        cleaned = _strip_leading_timestamp(content.strip())
        if cleaned:
            bubbles.append(cleaned)
    # More than MAX_BUBBLES requested: extra ones are dropped (logged by caller).
    return bubbles, ""


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

    def _current_tool_defs(self) -> List[Dict]:
        state = _get_presence_state().get("state", "rest")
        try:
            return get_tool_definitions(presence_state=state)
        except TypeError:
            # Old signature fallback (should not happen after migration).
            return get_tool_definitions()

    def _build_notification_block(self, batch_text: str) -> Optional[str]:
        """Explicit notification wrapper for rest/working.

        History (agent_internal_history) already carries 5-10+ previous
        messages within the token limit; this block marks which part is the
        NEW notification vs context so the model in rest can decide whether
        enter_dialog is worth it.
        """
        presence = _get_presence_state()
        state = presence.get("state", "rest")
        if state not in ("rest", "working"):
            return None
        last = (batch_text or "").strip()[:2000]
        if not last:
            return None
        return (
            f"[уведомление] новый текст из чата {self.chat_id}:\n"
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

    async def _execute_single_action(self, action: Dict) -> Dict:
        """Execute a single direct-tool action."""
        executor = ToolExecutor(
            text_memory=self.text_memory,
            embedding_client=self.emb_client,
            current_user_id=self.user_id,
            chat_id=self.chat_id,
        )
        return await executor.execute_action(action)

    @staticmethod
    def _attach_media(messages: List[Dict], media: Dict[str, Any],
                      default_chat_id: str = "") -> List[Dict]:
        """Attach native photo/audio to history + current event.

        - Current batch bytes go to the LAST user message (as before).
        - Recent history bytes (last 3 photos + last 2 audios from
          chunks.media_path) are re-attached to their own user messages,
          so the model still sees a photo a few turns later.
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
            # fold into the last user message so bytes aren't silently lost.
            leftover_imgs = [u for u, _p in hist_img_rev]
            leftover_auds = [a for a, _p in hist_aud_rev]
            if (leftover_imgs or leftover_auds) and last_user_idx is not None:
                base = messages[last_user_idx].get("content", "")
                if not isinstance(base, list):
                    note = str(base)
                    if leftover_imgs:
                        note += f"\n[ранее в диалоге было фото — смотри прикреплённое, всего {len(leftover_imgs)}]"
                    parts = build_multimodal_parts(note, leftover_imgs or None,
                                                   leftover_auds or None)
                    messages[last_user_idx] = {"role": "user", "content": parts}
                    leftover_imgs, leftover_auds = [], []

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
                                has_instr = any(
                                    isinstance(p, dict) and p.get("type") == "text"
                                    and "Слушай это голосовое" in str(p.get("text", ""))
                                    for p in base)
                                if not has_instr:
                                    from core.media import AUDIO_LISTEN_INSTRUCTION
                                    base.insert(0, {"type": "text",
                                                    "text": AUDIO_LISTEN_INSTRUCTION})
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
            n_img = len(image_urls) + len(hist_image_urls)
            n_aud = len(audios) + len(hist_audios)
            if n_img or n_aud:
                print(f"[Pipeline] media attached: {n_img} img + {n_aud} audio "
                      f"(current {len(image_urls)}+{len(audios)}, history "
                      f"{len(hist_image_urls)}+{len(hist_audios)})")
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
    ):
        """Yield low-level step dicts; caller translates to public events.

        Yields internal dicts:
          {"kind": "llm", "content": str, "tool_calls": [...], "finish_reason": str}
          {"kind": "action", "action": executed_dict}
          {"kind": "reply_bubbles", "texts": [...], "target_chat_id": str}
          {"kind": "reply_tool", "action": executed send_message dict}
          {"kind": "silence", "reason": str}
        """
        messages = list(agent_internal_history)
        notif_block = self._build_notification_block(user_message or "")
        if notif_block:
            messages.append({"role": "system", "content": notif_block})
        # Always attach: history media (last 3 photos + 2 audios) must load
        # even on text-only turns (media=None). chat_id fallback = self.chat_id.
        messages = self._attach_media(messages, media or {}, default_chat_id=self.chat_id)

        max_iterations = _max_iterations()
        choice = _tool_choice()
        iteration = 0
        use_response_format = True  # dialog JSON; disabled on provider error

        while iteration < max_iterations:
            iteration += 1
            presence = _get_presence_state()
            state = presence.get("state", "rest")
            print(f"\n── Iteration {iteration}/{max_iterations} [{state}] ──")

            # Presence may have changed mid-loop (enter_dialog etc.) —
            # rebuild prompt + tools every iteration so rules match state.
            main_system_prompt = build_main_llm_system_prompt(
                self.user_id, chat_id=self.chat_id, my_user_id=self.my_user_id
            )
            tool_definitions = self._current_tool_defs()
            want_json = state == "dialog"
            fmt = None
            if want_json and use_response_format:
                fmt = {"type": "json_schema", "json_schema": CHAT_MESSAGES_SCHEMA}

            try:
                response = await self.main_llm.generate(
                    messages=messages,
                    system_prompt=main_system_prompt,
                    tools=tool_definitions if tool_definitions else None,
                    tool_choice=choice if tool_definitions else "none",
                    max_tokens=2048,
                    temperature=self.main_llm.default_temperature,
                    response_format=fmt,
                )
            except Exception as e:
                # Provider without json_schema support: retry prompt-only once.
                if want_json and use_response_format and "response_format" in str(e).lower():
                    print(f"  [Pipeline] response_format rejected ({e}), retrying prompt-only")
                    use_response_format = False
                    response = await self.main_llm.generate(
                        messages=messages,
                        system_prompt=main_system_prompt,
                        tools=tool_definitions if tool_definitions else None,
                        tool_choice=choice if tool_definitions else "none",
                        max_tokens=2048,
                        temperature=self.main_llm.default_temperature,
                    )
                else:
                    raise

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
                    bubbles, err = parse_chat_messages(content)
                    if bubbles is None:
                        print(f"  [Dialog JSON invalid → retry: {err}]")
                        messages.append({"role": "system", "content": DIALOG_JSON_REMINDER})
                        if content.strip():
                            messages.append({"role": "assistant", "content": content})
                        if iteration >= max_iterations:
                            yield {"kind": "silence", "reason": f"bad dialog JSON: {err}"}
                            return
                        continue
                    if not bubbles:
                        yield {"kind": "silence", "reason": "empty messages array"}
                        return
                    target = str(presence.get("focus_chat_id") or self.chat_id)
                    yield {"kind": "reply_bubbles", "texts": bubbles,
                           "target_chat_id": target}
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
            presence_switched = False
            send_tool_reply = None
            sleep_started = False

            for action in actions:
                executed = await self._execute_single_action(action)
                get_needs_tracker().apply_action_effect(action.get("action_type"))
                yield {"kind": "action", "action": executed}

                atype = executed.get("type")
                status = executed.get("status")

                if atype == "start_sleep" and status == "sleeping":
                    sleep_started = True
                    break

                if atype in ("enter_dialog", "enter_rest", "enter_working") \
                        and status == "switched":
                    presence_switched = True
                    messages.append({"role": "system", "content": (
                        f"Presence switched: {atype} → "
                        f"{executed.get('chat_id', executed.get('state', ''))}. "
                        f"Decide in the new state now."
                    )})
                    print("  [Presence switched → re-calling]")
                    continue

                if atype == "send_message" and status == "ready":
                    if (executed.get("text") or "").strip():
                        send_tool_reply = executed
                    continue

                if atype == "search_memory":
                    has_search = True
                    if status == "searched":
                        context_addon = executed.get("context_addon", "[No results found]")
                        messages.append({"role": "system",
                                         "content": f"Search results in memory:\n{context_addon}"})
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

            if sleep_started:
                yield {"kind": "silence", "reason": "sleep started"}
                return

            if send_tool_reply is not None and not has_search and not presence_switched:
                # Single note in rest/working — final, no re-call.
                yield {"kind": "reply_tool", "action": send_tool_reply}
                return

            if has_search or presence_switched:
                continue

            # Diary-only turn with no verdict yet: re-call.
            if iteration >= max_iterations:
                yield {"kind": "silence", "reason": "tools with no reply"}
                return
            messages.append({"role": "system", "content": (
                "No answer yet. "
                + (DIALOG_JSON_REMINDER if _get_presence_state().get("state") == "dialog"
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
    ) -> Dict[str, Any]:
        total_start = time.time()
        try:
            all_actions: List[Dict] = []
            raw_llm_responses: List[Dict] = []
            reply_texts: List[str] = []
            reply_text = ""
            reply_via = ""
            reply_chat_id = ""
            silence_reason = ""
            iterations = 0

            async for step in self._run_stream(user_message, agent_internal_history, media):
                kind = step.get("kind")
                if kind == "llm":
                    iterations = step.get("iteration", iterations)
                    raw_llm_responses.append({
                        "iteration": step.get("iteration"),
                        "thoughts": step.get("content", ""),
                        "tool_calls": step.get("tool_calls", []),
                        "finish_reason": step.get("finish_reason"),
                    })
                elif kind == "action":
                    all_actions.append(step["action"])
                elif kind == "reply_bubbles":
                    reply_texts = list(step["texts"])
                    reply_text = "\n".join(reply_texts)
                    reply_chat_id = step["target_chat_id"]
                    reply_via = "bubbles"
                    get_needs_tracker().apply_action_effect("send_message")
                    break
                elif kind == "reply_tool":
                    a = step["action"]
                    reply_text = (a.get("text") or "").strip()
                    reply_chat_id = str(a.get("chat_id", self.chat_id))
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
            await self._save_log(result, user_message)
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
            await self._save_log({**error_result, "traceback": tb}, user_message)
            return error_result

    async def _save_log(self, result: Dict, user_message: str):
        """Save a detailed JSON log of the pipeline run."""
        log_dir = settings.LOG_DIR
        os.makedirs(log_dir, exist_ok=True)

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": self.user_id,
            "chat_id": self.chat_id,
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
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Streaming version consumed by core/queue.py.

        Yields:
          {"type": "thought", "thoughts": str}
          {"type": "action", "action": Dict}
          {"type": "search_result", "content": str}
          {"type": "reply", "texts": [...], "chat_id": str, "via": str}
          {"type": "silence", "reason": str}
          {"type": "done", "result": Dict}
          {"type": "error", "error": str}
        """
        total_start = time.time()
        try:
            all_actions: List[Dict] = []
            raw_llm_responses: List[Dict] = []
            reply_texts: List[str] = []
            reply_text = ""
            reply_via = ""
            reply_chat_id = ""
            silence_reason = ""
            iteration = 0

            async for step in self._run_stream(user_message, agent_internal_history, media):
                kind = step.get("kind")
                if kind == "llm":
                    iteration = step.get("iteration", iteration)
                    raw_llm_responses.append({
                        "iteration": step.get("iteration"),
                        "thoughts": step.get("content", ""),
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
                elif kind == "reply_bubbles":
                    reply_texts = list(step["texts"])
                    reply_text = "\n".join(reply_texts)
                    reply_chat_id = step["target_chat_id"]
                    reply_via = "bubbles"
                    get_needs_tracker().apply_action_effect("send_message")
                    yield {"type": "reply", "texts": reply_texts, "text": reply_text,
                           "chat_id": reply_chat_id, "via": "bubbles"}
                    break
                elif kind == "reply_tool":
                    a = step["action"]
                    # _run_stream already applied needs effect; avoid double count
                    # for the action itself (applied inside _run_stream), so just yield.
                    reply_text = (a.get("text") or "").strip()
                    reply_texts = [reply_text] if reply_text else []
                    reply_chat_id = str(a.get("chat_id", self.chat_id))
                    reply_via = "send_message_brush_off"
                    yield {"type": "reply", "texts": reply_texts, "text": reply_text,
                           "chat_id": reply_chat_id, "via": "send_message_brush_off"}
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
            await self._save_log(result, user_message)
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
            await self._save_log({**error_result, "traceback": tb}, user_message)
            yield {"type": "error", "error": str(e)}


# Backward/forward-compatible alias.
MilaPipeline = ThinkPipeline

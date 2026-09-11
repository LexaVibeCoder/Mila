"""
Direct-tool reasoning pipeline (no Think wrapper).

New scheme:
  rest/working --notification(last + 5-10 prev)--> enter_dialog -->
  dialog --[search_memory/diary directly]--> reply in `content`.

- tool_choice is always "auto": the model herself decides when a normal
  tool is needed. Forcing "required" would break content-replies.
- In dialog: reply = `content`, send_message does NOT exist.
- In rest/working: full reply ONLY after enter_dialog; single brush-off
  via send_message tool; empty content + no tools = silence.
- Silence is empty content, NOT a tool (silent-token pattern).

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


# Reminder when rest/working returns bare content without entering dialog.
REST_CONTENT_REMINDER = (
    "PROTOCOL: you are in rest/working. Bare text in `content` is IGNORED here. "
    "To answer fully, call enter_dialog with the notification chat_id first "
    "(you will be called again in dialog, then answer in `content`). "
    "For a one-line brush-off use the send_message tool once. "
    "To stay silent return empty content with no tool calls."
)


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
            f"(выше в истории — предыдущие ~5-10 сообщений этого чата для контекста. "
            f"Ты в {state}: полноценный ответ только через enter_dialog, "
            f"разовая отмашка через send_message, молчание — пустой content.)"
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
    def _attach_media(messages: List[Dict], media: Dict[str, Any]) -> List[Dict]:
        """Attach native photo/audio to the LAST user message (current event).

        History stays text-only (placeholders); only the current notification
        carries bytes. Falls back to text-only if nothing usable.
        """
        try:
            from core.media import build_multimodal_parts
            image_urls = media.get("image_data_urls") or []
            audio = media.get("audio")
            event_line = media.get("event_line", "")
            if not image_urls and audio is None:
                if event_line:
                    messages = list(messages)
                    messages.append({"role": "system", "content": event_line})
                return messages
            messages = list(messages)
            # Find last user message and enrich it
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    base = messages[i].get("content", "")
                    if isinstance(base, list):
                        # Already multimodal — leave as is
                        break
                    parts = build_multimodal_parts(str(base), image_urls, audio)
                    messages[i] = {"role": "user", "content": parts}
                    break
            if event_line:
                messages.append({"role": "system", "content": event_line})
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
          {"kind": "reply_content", "text": str, "target_chat_id": str}
          {"kind": "reply_tool", "action": executed send_message dict}
          {"kind": "silence", "reason": str}
        """
        messages = list(agent_internal_history)
        notif_block = self._build_notification_block(user_message or "")
        if notif_block:
            messages.append({"role": "system", "content": notif_block})
        if media:
            messages = self._attach_media(messages, media)

        max_iterations = _max_iterations()
        choice = _tool_choice()
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            print(f"\n── Iteration {iteration}/{max_iterations} [direct] ──")

            # Presence may have changed mid-loop (enter_dialog etc.) —
            # rebuild prompt + tools every iteration so rules match state.
            presence = _get_presence_state()
            state = presence.get("state", "rest")
            main_system_prompt = build_main_llm_system_prompt(
                self.user_id, chat_id=self.chat_id, my_user_id=self.my_user_id
            )
            tool_definitions = self._current_tool_defs()

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
                "tool_calls": [
                    {"name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (tool_calls or [])
                ],
                "finish_reason": response.get("finish_reason"),
                "system_prompt": main_system_prompt,
            }

            actions = self._tool_calls_to_actions(tool_calls)

            # ---- No tools: content is the verdict ----
            if not actions:
                text = content.strip()
                if not text:
                    yield {"kind": "silence", "reason": "empty content, no tools"}
                    return
                if state == "dialog":
                    target = str(presence.get("focus_chat_id") or self.chat_id)
                    yield {"kind": "reply_content", "text": text,
                           "target_chat_id": target}
                    return
                # rest/working + bare content = protocol violation -> retry
                print("  [Protocol: bare content in rest/working → reminder]")
                messages.append({"role": "system", "content": REST_CONTENT_REMINDER})
                # Keep the model's text for logs but do NOT send it.
                messages.append({"role": "assistant", "content": content})
                if iteration >= max_iterations:
                    yield {"kind": "silence",
                           "reason": "bare content in rest without enter_dialog"}
                    return
                continue

            # ---- Tools present: content this turn is NOT sent ----
            # (When the model calls tools, content is reasoning/draft.)
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

                if atype == "silence":
                    # Backward compat: old silence tool if model somehow calls it.
                    yield {"kind": "silence",
                           "reason": executed.get("reason", "silence tool")}
                    return

                if atype == "start_sleep" and status == "sleeping":
                    sleep_started = True
                    break

                if atype in ("enter_dialog", "enter_rest", "enter_working") \
                        and status == "switched":
                    presence_switched = True
                    messages.append({"role": "system", "content": (
                        f"Presence switched: {atype} → "
                        f"{executed.get('chat_id', executed.get('state', ''))}. "
                        f"Now decide what to do in the new state: answer in "
                        f"`content` (dialog) or stay silent (empty content)."
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
                            "Answer from the conversation context or stay silent."
                        )})
                        print("  [Search error → re-calling anyway]")
                    continue

                if atype in ("write_diary", "update_diary"):
                    if status in ("written", "updated"):
                        messages.append({"role": "system", "content": (
                            f"Diary {atype} done ({status}). "
                            f"Now answer in `content` or stay silent."
                        )})
                    else:
                        messages.append({"role": "system", "content": (
                            f"Diary {atype} failed ({executed.get('error', status)}). "
                            f"Answer from context or stay silent."
                        )})
                    continue

            if sleep_started:
                yield {"kind": "silence", "reason": "sleep started"}
                return

            if send_tool_reply is not None and not has_search and not presence_switched:
                # Single brush-off in rest/working — final, no re-call.
                yield {"kind": "reply_tool", "action": send_tool_reply}
                return

            if has_search or presence_switched:
                continue

            # Only diary tools (+ draft content ignored): re-call for final verdict,
            # unless model already gave content alongside diary calls.
            if content.strip():
                text = content.strip()
                presence2 = _get_presence_state()
                if presence2.get("state") == "dialog":
                    target = str(presence2.get("focus_chat_id") or self.chat_id)
                    yield {"kind": "reply_content", "text": text,
                           "target_chat_id": target}
                    return
                # rest + diary + content but no enter_dialog -> still violation
                messages.append({"role": "system", "content": REST_CONTENT_REMINDER})
                continue

            # Tools with no outcome (e.g. diary error + empty content): re-call.
            if iteration >= max_iterations:
                yield {"kind": "silence", "reason": "tools with no reply"}
                return
            messages.append({"role": "system", "content": (
                "No reply yet. Answer in `content` now or stay silent "
                "(empty content, no tools)."
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
                elif kind == "reply_content":
                    reply_text = step["text"]
                    reply_chat_id = step["target_chat_id"]
                    reply_via = "content"
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
          {"type": "reply", "text": str, "chat_id": str, "via": str}
          {"type": "silence", "reason": str}
          {"type": "done", "result": Dict}
          {"type": "error", "error": str}
        """
        total_start = time.time()
        try:
            all_actions: List[Dict] = []
            raw_llm_responses: List[Dict] = []
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
                elif kind == "reply_content":
                    reply_text = step["text"]
                    reply_chat_id = step["target_chat_id"]
                    reply_via = "content"
                    get_needs_tracker().apply_action_effect("send_message")
                    yield {"type": "reply", "text": reply_text,
                           "chat_id": reply_chat_id, "via": "content"}
                    break
                elif kind == "reply_tool":
                    a = step["action"]
                    # _run_stream already applied needs effect; avoid double count
                    # for the action itself (applied inside _run_stream), so just yield.
                    reply_text = (a.get("text") or "").strip()
                    reply_chat_id = str(a.get("chat_id", self.chat_id))
                    reply_via = "send_message_brush_off"
                    yield {"type": "reply", "text": reply_text,
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

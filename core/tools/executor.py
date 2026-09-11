import json
from typing import List, Dict, Any, Optional


def get_tool_definitions(presence_state: str = "rest") -> List[Dict]:
    """Direct tool definitions (no Think wrapper).

    New scheme:
    - dialog: reply = `content`, NO send_message. Tools called directly.
    - rest/working: to start a full dialogue you MUST call enter_dialog first.
      send_message exists ONLY for a single brush-off
      ("я занята, давай позже") without entering dialog.
    - silence: NO tool. Empty `content` + no tool calls = stay silent.
      (Best practice for tool_choice=auto: model answers in content or
      calls a tool; forcing a silence tool wastes a turn and causes
      hallucinations under tool_choice=required. See DiscussLLM silent-token
      pattern + OpenAI auto vs required docs.)

    presence_state filters send_message availability so the model cannot
    bypass dialog via send_message while in dialog.
    """
    try:
        from Mila_observatory.runtime import runtime_config
        tools_enabled = runtime_config.get("tools", {})
        search_enabled = tools_enabled.get("search_memory", True)
        send_enabled = tools_enabled.get("send_message", True)
        diary_enabled = tools_enabled.get("write_diary", True)
        diary_update_enabled = tools_enabled.get("update_diary", True)
        presence_enabled = tools_enabled.get("presence", True)
    except ImportError:
        search_enabled = True
        send_enabled = True
        diary_enabled = True
        diary_update_enabled = True
        presence_enabled = True

    # NOTE: tools.silence is intentionally ignored — silence is empty content,
    # not a tool. Kept in runtime JSON for backward compat, has no effect.

    defs: List[Dict] = []

    if search_enabled:
        defs.append({
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search long-term memory for facts about the user. "
                               "Call directly when you need context. Results come back "
                               "as a system message, then answer in `content`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look for in memory",
                        }
                    },
                    "required": ["query"],
                },
            },
        })

    # send_message ONLY outside dialog — single brush-off, never a dialogue.
    if send_enabled and presence_state in ("rest", "working"):
        defs.append({
            "type": "function",
            "function": {
                "name": "send_message",
                "description": "Send ONE single brush-off message WITHOUT entering dialog. "
                               "Use ONLY in rest/working when you are busy and want to send "
                               "a one-liner like 'я занята, давай позже'. "
                               "NEVER use for a full dialogue — for dialogue call "
                               "enter_dialog first and then answer in `content`. "
                               "Not available in dialog state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "Target chat ID (defaults to current notification chat)",
                        },
                        "text": {
                            "type": "string",
                            "description": "One short brush-off line in Mila's voice",
                        },
                    },
                    "required": ["text"],
                },
            },
        })

    if diary_enabled:
        defs.append({
            "type": "function",
            "function": {
                "name": "write_diary",
                "description": "Write a diary entry (1-3 short sentences, first person). "
                               "Call directly, then continue with your reply in `content`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry": {
                            "type": "string",
                            "description": "Diary entry text",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["day_entries", "reflection_entries"],
                            "description": "'day_entries' — events, 'reflection_entries' — thoughts/feelings",
                        },
                    },
                    "required": ["entry"],
                },
            },
        })

    if diary_update_enabled:
        defs.append({
            "type": "function",
            "function": {
                "name": "update_diary",
                "description": "Update an existing diary entry by ID (take ID from diary block).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry": {
                            "type": "string",
                            "description": "New entry text",
                        },
                        "entry_id": {
                            "type": "string",
                            "description": "ID from diary block, e.g. '2'",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["day_entries", "reflection_entries"],
                        },
                    },
                    "required": ["entry", "entry_id"],
                },
            },
        })

    if presence_enabled:
        defs.append({
            "type": "function",
            "function": {
                "name": "enter_dialog",
                "description": "Move into dialog with a specific chat. REQUIRED in rest/working "
                               "before answering a notification with a full reply. "
                               "After the switch you will be called again in dialog state — "
                               "then answer in `content`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "Chat to talk to (take from the notification)",
                        },
                        "note": {
                            "type": "string",
                            "description": "Why you enter dialog (optional)",
                        },
                    },
                    "required": ["chat_id"],
                },
            },
        })
        defs.append({
            "type": "function",
            "function": {
                "name": "enter_rest",
                "description": "Return to rest (default state) when dialogue is over and there is nothing to do.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "Optional reason"},
                    },
                },
            },
        })
        defs.append({
            "type": "function",
            "function": {
                "name": "enter_working",
                "description": "Get busy with your own stuff (texts for publishing house). "
                               "Same notification visibility as rest in v1.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "What you are working on"},
                    },
                },
            },
        })
        defs.append({
            "type": "function",
            "function": {
                "name": "start_sleep",
                "description": "Go to sleep and start the night cycle (reflection, memorization).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "duration_minutes": {
                            "type": "integer",
                            "description": "How long to sleep in minutes, e.g. 480",
                        },
                        "note": {"type": "string", "description": "Optional note"},
                    },
                    "required": ["duration_minutes"],
                },
            },
        })

    # Always return at least one tool so tool_choice=auto works;
    # if everything is disabled the model just answers in content / stays silent.
    # (Old fallback that forced a `silence` enum entry is removed with Think.)
    return defs


class ToolExecutor:
    def __init__(
        self,
        text_memory,
        embedding_client,
        current_user_id: str,
        chat_id: str,
    ):
        self.text_memory = text_memory
        self.emb_client = embedding_client
        self.current_user_id = current_user_id
        self.chat_id = chat_id

    async def execute_action(self, action: Dict) -> Dict:
        action_type = action.get("action_type", "")
        params = action.get("params", {})

        if action_type == "send_message":
            return await self._handle_send_message(params)
        elif action_type == "search_memory":
            return await self._handle_search_memory(params)
        elif action_type == "silence":
            return await self._handle_silence(params)
        elif action_type == "write_diary":
            return await self._handle_write_diary(params)
        elif action_type == "update_diary":
            return await self._handle_update_diary(params)
        elif action_type == "enter_dialog":
            return await self._handle_enter_dialog(params)
        elif action_type == "enter_rest":
            return await self._handle_enter_rest(params)
        elif action_type == "enter_working":
            return await self._handle_enter_working(params)
        elif action_type == "start_sleep":
            return await self._handle_start_sleep(params)
        else:
            return {
                "type": action_type,
                "status": "unknown",
                "error": f"Unknown action: {action_type}"
            }

    async def _handle_send_message(self, params: Dict) -> Dict:
        text = params.get("text", "")
        chat_id = params.get("chat_id", self.chat_id)
        return {
            "type": "send_message",
            "text": text,
            "chat_id": chat_id,
            "status": "ready"
        }

    async def _handle_search_memory(self, params: Dict) -> Dict:
        query = params.get("query", "")

        from core.llm.reranker import RerankerClient
        from core.db import search_memories_by_faiss_ids

        try:
            vec = await self.emb_client.embed([query])
            vec = vec[0]

            distances, indices = self.text_memory.search(vec, k=10)
            valid_indices = [int(idx) for idx in indices if idx != -1]

            results = None
            context_addon = "[Nothing found in memory]"

            if valid_indices:
                memories = search_memories_by_faiss_ids(valid_indices)
                if memories:
                    reranker = RerankerClient()
                    documents = [m.get("content", "") for m in memories]
                    reranked = await reranker.rerank(query, documents)

                    seen = set()
                    results = []
                    for doc, score in reranked:
                        for mem in memories:
                            if mem.get("content") == doc and mem.get("mem_id") not in seen:
                                results.append({"content": doc, "score": score, **mem})
                                seen.add(mem.get("mem_id"))
                                break

                    context_lines = ["[Search results in memory:]"]
                    for r in results[:5]:
                        preview = r.get("content", "")[:200]
                        context_lines.append(f"- {preview} (relevance: {r.get('score', 0):.2f})")
                    context_addon = "\n".join(context_lines)

            print(f"[ToolExecutor] Search: '{query}' → {len(results or [])} results")
            return {
                "type": "search_memory",
                "query": query,
                "results": results,
                "context_addon": context_addon,
                "status": "searched"
            }

        except Exception as e:
            print(f"[ToolExecutor] Search error: {e}")
            return {
                "type": "search_memory",
                "query": query,
                "results": None,
                "context_addon": "[Error searching memory]",
                "status": "error",
                "error": str(e)
            }

    async def _handle_silence(self, params: Dict) -> Dict:
        reason = params.get("reason", "")
        print(f"[ToolExecutor] Silence: {reason}")
        return {
            "type": "silence",
            "reason": reason,
            "status": "silent"
        }

    def _extract_entry_text(self, params: Dict) -> str:
        """Diary text may be passed as 'entry' or 'text' — accept both to avoid
        model mistakes between the send_message ('text') and diary ('entry') params."""
        entry = params.get("entry", "")
        if not entry or not entry.strip():
            entry = params.get("text", "")
        return (entry or "").strip()

    async def _handle_write_diary(self, params: Dict) -> Dict:
        entry = self._extract_entry_text(params)
        if not entry:
            return {
                "type": "write_diary",
                "status": "error",
                "error": "Missing diary entry text (param 'entry')"
            }

        from core.diary.manager import add_diary_entry
        category = params.get("category", "day_entries")
        result = add_diary_entry(entry.strip(), category=category)
        print(f"[ToolExecutor] Diary entry #{result['entry_number']} (ID: {result['entry_id']}, {result['category']}) written")
        return {
            "type": "write_diary",
            "entry_number": result["entry_number"],
            "entry_id": result["entry_id"],
            "category": result["category"],
            "status": "written"
        }

    async def _handle_update_diary(self, params: Dict) -> Dict:
        entry = self._extract_entry_text(params)
        entry_id = params.get("entry_id", "")
        if not entry:
            return {
                "type": "update_diary",
                "status": "error",
                "error": "Missing diary entry text (param 'entry')"
            }
        if not str(entry_id).strip():
            return {
                "type": "update_diary",
                "status": "error",
                "error": "Missing entry_id"
            }

        from core.diary.manager import update_diary_entry
        category = params.get("category")
        updated = update_diary_entry(int(entry_id), entry.strip(), category=category)
        if not updated:
            return {
                "type": "update_diary",
                "entry_id": entry_id,
                "status": "not_found",
                "error": f"Diary entry ID {entry_id} not found"
            }
        print(f"[ToolExecutor] Diary entry ID {entry_id} updated")
        return {
            "type": "update_diary",
            "entry_id": entry_id,
            "status": "updated"
        }

    # ------------------------------------------------------------------
    # Presence tools — Mila switches her own state.
    # ------------------------------------------------------------------

    async def _handle_enter_dialog(self, params: Dict) -> Dict:
        from core.presence import enter_dialog
        from core.db import get_user_display_name
        chat_id = str(params.get("chat_id", self.chat_id or "")).strip()
        if not chat_id:
            return {"type": "enter_dialog", "status": "error",
                    "error": "Missing chat_id"}
        note = str(params.get("note", "") or "")
        try:
            user_name = get_user_display_name(chat_id)
        except Exception:
            user_name = chat_id
        state = enter_dialog(chat_id, user_name, note)
        print(f"[Presence] → dialog with {user_name} (chat {chat_id})")
        return {"type": "enter_dialog", "chat_id": chat_id,
                "user_name": user_name, "state": state, "status": "switched"}

    async def _handle_enter_rest(self, params: Dict) -> Dict:
        from core.presence import enter_rest
        note = str(params.get("note", "") or "")
        state = enter_rest(note)
        print("[Presence] → rest")
        return {"type": "enter_rest", "state": state, "status": "switched"}

    async def _handle_enter_working(self, params: Dict) -> Dict:
        from core.presence import enter_working
        note = str(params.get("note", "") or "")
        state = enter_working(note)
        print(f"[Presence] → working ({note[:60]})")
        return {"type": "enter_working", "state": state, "status": "switched"}

    async def _handle_start_sleep(self, params: Dict) -> Dict:
        from core.presence import enter_sleep_note
        try:
            duration = int(params.get("duration_minutes", 0) or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            return {"type": "start_sleep", "status": "error",
                    "error": "Missing duration_minutes (how long to sleep)"}
        duration = max(1, min(duration, 12 * 60))
        note = str(params.get("note", "") or "")
        enter_sleep_note(note or f"{duration} min")
        try:
            from core.sleep.manager import start_sleep
            result = await start_sleep(duration_minutes=duration,
                                       triggered_by="mila_tool")
        except Exception as e:
            return {"type": "start_sleep", "status": "error", "error": str(e)}
        print(f"[Presence] → sleep for {duration} min")
        return {"type": "start_sleep", "duration_minutes": duration,
                "sleep_result": result, "status": "sleeping"}

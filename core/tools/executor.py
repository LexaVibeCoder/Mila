import json
from typing import List, Dict, Any, Optional


def get_tool_definitions() -> List[Dict]:
    try:
        from Mila_observatory.runtime import runtime_config
        tools_enabled = runtime_config.get("tools", {})
        search_enabled = tools_enabled.get("search_memory", True)
        send_enabled = tools_enabled.get("send_message", True)
        silence_enabled = tools_enabled.get("silence", True)
        diary_enabled = tools_enabled.get("write_diary", True)
        diary_update_enabled = tools_enabled.get("update_diary", True)
        presence_enabled = tools_enabled.get("presence", True)
    except ImportError:
        search_enabled = True
        send_enabled = True
        silence_enabled = True
        diary_enabled = True
        diary_update_enabled = True
        presence_enabled = True

    action_enum = []
    if send_enabled:
        action_enum.append("send_message")
    if search_enabled:
        action_enum.append("search_memory")
    if silence_enabled:
        action_enum.append("silence")
    if diary_enabled:
        action_enum.append("write_diary")
    if diary_update_enabled:
        action_enum.append("update_diary")
    if presence_enabled:
        action_enum.extend(["enter_dialog", "enter_rest", "enter_working", "start_sleep"])

    if not action_enum:
        action_enum.append("silence")

    diary_categories_enum = ["day_entries", "reflection_entries"]

    return [
        {
            "type": "function",
            "function": {
                "name": "think",
                "description": "Express your internal thoughts and optionally perform actions. "
                               "You MUST call this function EVERY turn. "
                               "'thoughts' is your private reasoning (never visible to user). "
                               "'actions' is a list of actions you want to perform. "
                               "CRITICAL: In the JSON object, the 'thoughts' key MUST appear BEFORE the 'actions' key. "
                               "Always write 'thoughts' first, then 'actions'. "
                               "Never start with actions or mix actions into thoughts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thoughts": {
                            "type": "string",
                            "description": "Your private reasoning and internal monologue. "
                                           "Think about what the user said, what you know, "
                                           "and what you want to do next."
                        },
                        "actions": {
                            "type": "array",
                            "description": "List of actions to perform. "
                                           "If you want to reply to the user, include a send_message action. "
                                           "If you need more information from memory, include search_memory. "
                                           "If you want to stay silent (not reply), include a silence action. "
                                           "If you want to write or update your diary, include write_diary or update_diary.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action_type": {
                                        "type": "string",
                                        "enum": action_enum,
                                        "description": "Type of action to perform"
                                    },
                                    "params": {
                                        "type": "object",
                                        "description": "Parameters for the action",
                                        "properties": {
                                            "chat_id": {
                                                "type": "string",
                                                "description": "Telegram chat ID to send message to. "
                                                               "Use the chat_id from context if replying to current conversation. "
                                                               "When searching memory, not needed."
                                            },
                                            "text": {
                                                "type": "string",
                                                "description": "Message text to send to the user (for send_message only). "
                                                               "Do NOT use 'text' for diary actions — diary uses 'entry'."
                                            },
                                            "query": {
                                                "type": "string",
                                                "description": "Search query to look up in memory (for search_memory only)"
                                            },
                                            "reason": {
                                                "type": "string",
                                                "description": "Reason for silence (for silence action only). "
                                                               "Explain why you chose not to respond."
                                            },
                                            "entry": {
                                                "type": "string",
                                                "description": "Diary entry content (for write_diary / update_diary only). "
                                                               "1-3 short sentences, first person, natural. "
                                                               "Use the 'entry' param for diary actions (not 'text')."
                                            },
                                            "category": {
                                                "type": "string",
                                                "enum": diary_categories_enum,
                                                "description": "Diary entry category (for write_diary / update_diary only). "
                                                               "'day_entries' — what happened during the day (events, conversations, moments). "
                                                               "'reflection_entries' — your thoughts and feelings about what happened."
                                            },
                                            "entry_id": {
                                                "type": "string",
                                                "description": "ID of an existing diary entry to update (for update_diary only). "
                                                               "Take it from the diary block in the system prompt, e.g. '[ID: 2]'."
                                            },
                                            "duration_minutes": {
                                                "type": "integer",
                                                "description": "Sleep duration in minutes (for start_sleep only). "
                                                               "Required — how long Mila wants to sleep."
                                            },
                                            "note": {
                                                "type": "string",
                                                "description": "Short note/reason for presence tools "
                                                               "(enter_dialog / enter_rest / enter_working / start_sleep)."
                                            }
                                        }
                                    }
                                },
                                "required": ["action_type", "params"]
                            }
                        }
                    },
                    "required": ["thoughts", "actions"]
                }
            }
        }
    ]


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

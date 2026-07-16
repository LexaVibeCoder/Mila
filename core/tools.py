# core/tools.py
"""
Tool definitions and executor for the single-stage pipeline.
Model calls `think(thoughts, actions)` — system executes actions.
"""

import json
from typing import List, Dict, Any, Optional


def get_tool_definitions() -> List[Dict]:
    """OpenAI-compatible tool definitions вЂ” single `think` function.
    
    Reads runtime config to determine which tools are enabled/disabled.
    """
    # Check runtime config for tool enablement
    try:
        from Mila_observatory.runtime import runtime_config
        tools_enabled = runtime_config.get("tools", {})
        search_enabled = tools_enabled.get("search_memory", True)
        send_enabled = tools_enabled.get("send_message", True)
        silence_enabled = tools_enabled.get("silence", True)
        diary_enabled = tools_enabled.get("write_diary", True)
    except ImportError:
        # Observatory not installed — use defaults
        search_enabled = True
        send_enabled = True
        silence_enabled = True
        diary_enabled = True

    # Build action enum dynamically
    action_enum = []
    if send_enabled:
        action_enum.append("send_message")
    if search_enabled:
        action_enum.append("search_memory")
    if silence_enabled:
        action_enum.append("silence")
    if diary_enabled:
        action_enum.append("write_diary")

    # If no tools are enabled, at least allow silence
    if not action_enum:
        action_enum.append("silence")

    return [
        {
            "type": "function",
            "function": {
                "name": "think",
                "description": "Express your internal thoughts and optionally perform actions. "
                               "You MUST call this function EVERY turn. "
                               "'thoughts' is your private reasoning (never visible to user). "
                               "'actions' is a list of actions you want to perform. "
                               "IMPORTANT: Always write 'thoughts' FIRST, then 'actions'. "
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
                                           "If you want to write a diary entry about this moment, include write_diary.",
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
                                                "description": "Message text to send (for send_message only)"
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
                                                "description": "Diary entry content (for write_diary only). "
                                                               "Write what happened or what you're feeling. "
                                                               "This is a personal diary entry, not a message to anyone."
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
    """Executes actions from the `think` function call."""

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
        """
        Execute a single action from the think() response.

        Args:
            action: dict with action_type and params

        Returns:
            Action result dict with type, status, and action-specific fields.
        """
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

        from core.reranker import RerankerClient
        from core.db import search_memories_by_faiss_ids

        try:
            vec = await self.emb_client.embed([query])
            vec = vec[0]

            distances, indices = self.text_memory.search(vec, k=10)
            valid_indices = [int(idx) for idx in indices if idx != -1]

            results = None
            context_addon = "[Ничего не найдено по запросу]"

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

                    context_lines = ["[Результаты поиска в памяти:]"]
                    for r in results[:5]:
                        preview = r.get("content", "")[:200]
                        context_lines.append(f"- {preview} (релевантность: {r.get('score', 0):.2f})")
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
                "context_addon": "[Ошибка поиска в памяти]",
                "status": "error",
                "error": str(e)
            }

    async def _handle_silence(self, params: Dict) -> Dict:
        """Handle silence action — LLM chose not to respond."""
        reason = params.get("reason", "")
        print(f"[ToolExecutor] Silence: {reason}")
        return {
            "type": "silence",
            "reason": reason,
            "status": "silent"
        }

    async def _handle_write_diary(self, params: Dict) -> Dict:
        """Handle write_diary action — LLM writes a conscious diary entry."""
        entry = params.get("entry", "")
        if not entry.strip():
            return {
                "type": "write_diary",
                "status": "error",
                "error": "Empty diary entry"
            }
        
        from core.diary import add_diary_entry
        entry_num = add_diary_entry(entry.strip())
        print(f"[ToolExecutor] Diary entry #{entry_num} written")
        return {
            "type": "write_diary",
            "entry_number": entry_num,
            "status": "written"
        }

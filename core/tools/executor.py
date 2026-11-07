import asyncio
import json
from typing import List, Dict, Any, Optional


def get_tool_definitions(presence_state: str = "rest",
                         include_proactive: bool = False) -> List[Dict]:
    """Direct tools for the current state only.

    - rest: enter_dialog (answer), send_message (one brush-off),
      enter_working, start_sleep.
    - working: enter_dialog, send_message, enter_rest, start_sleep.
    - dialog: search_memory, write_diary, update_diary, enter_dialog (switch),
      enter_rest, enter_working, start_sleep.
    - sleep: start_sleep only (pipeline rarely runs here).
    - proactive: rest/working + peek_recent_chats (one shot view of all chats).
    """
    try:
        from Mila_observatory.runtime import runtime_config
        tools_enabled = runtime_config.get("tools", {})
        search_enabled = tools_enabled.get("search_memory", True)
        send_enabled = tools_enabled.get("send_message", True)
        diary_enabled = tools_enabled.get("write_diary", True)
        diary_update_enabled = tools_enabled.get("update_diary", True)
        presence_enabled = tools_enabled.get("presence", True)
        peek_enabled = tools_enabled.get("peek_recent", True)
    except ImportError:
        search_enabled = True
        send_enabled = True
        diary_enabled = True
        diary_update_enabled = True
        presence_enabled = True
        peek_enabled = True

    state = presence_state or "rest"
    defs: List[Dict] = []

    if state == "dialog":
        if send_enabled:
            defs.append({
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "Send one short note to another chat without switching dialogs.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_alias": {"type": "string", "description": "Target chat alias"},
                            "text": {"type": "string", "description": "One short line"},
                        },
                        "required": ["chat_alias", "text"],
                    },
                },
            })
        if search_enabled:
            defs.append({
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "Find facts about the person in long-term memory. "
                                   "Results return as a system message.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "What to find"},
                        },
                        "required": ["query"],
                    },
                },
            })
        if diary_enabled:
            defs.append({
                "type": "function",
                "function": {
                    "name": "write_diary",
                    "description": "Save a meaningful moment or feeling to today's diary.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entry": {"type": "string", "description": "1-3 short sentences, first person"},
                            "category": {
                                "type": "string",
                                "enum": ["day_entries", "reflection_entries"],
                                "description": "day_entries: events. reflection_entries: feelings",
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
                    "description": "Extend an existing diary entry (ID from the diary block).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entry": {"type": "string", "description": "New text"},
                            "entry_id": {"type": "string", "description": "Entry ID, e.g. '2'"},
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
                    "description": "Switch to a chat using its alias and explain why.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_alias": {"type": "string", "description": "Alias of the chat to switch to"},
                            "note": {"type": "string", "description": "Reason for entering this dialog"},
                        },
                        "required": ["chat_alias", "note"],
                    },
                },
            })
            defs.append({
                "type": "function",
                "function": {
                    "name": "enter_rest",
                    "description": "Finish the dialogue and rest.",
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
                    "description": "Pause the dialogue and do your own work.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "note": {"type": "string", "description": "What you work on"},
                        },
                    },
                },
            })
            defs.append({
                "type": "function",
                "function": {
                    "name": "start_sleep",
                    "description": "Go to sleep for a while.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "duration_minutes": {
                                "type": "integer",
                                "description": "Minutes of sleep, e.g. 480",
                            },
                            "note": {"type": "string", "description": "Optional note"},
                        },
                        "required": ["duration_minutes"],
                    },
                },
            })
        return defs

    # rest / working / sleep and fallback
    if presence_enabled:
        if state in ("rest", "working", "sleep"):
            target_desc = "a chat from the available list"
            defs.append({
                "type": "function",
                "function": {
                    "name": "enter_dialog",
                    "description": f"Start talking to {target_desc}.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_alias": {"type": "string", "description": "Chat alias from the available list"},
                            "note": {"type": "string", "description": "Reason for entering this dialog"},
                        },
                        "required": ["chat_alias", "note"],
                    },
                },
            })
        if send_enabled and state in ("rest", "working"):
            defs.append({
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "Send one short note without starting a conversation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_alias": {"type": "string", "description": "Target chat alias; omit only when replying to a notification"},
                            "text": {"type": "string", "description": "One short line in your voice"},
                        },
                        "required": ["text"],
                    },
                },
            })
        if state == "working":
            defs.append({
                "type": "function",
                "function": {
                    "name": "enter_rest",
                    "description": "Stop working and rest.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "note": {"type": "string", "description": "Optional reason"},
                        },
                    },
                },
            })
        if state == "rest":
            defs.append({
                "type": "function",
                "function": {
                    "name": "enter_working",
                    "description": "Do your own work for a while.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "note": {"type": "string", "description": "What you work on"},
                        },
                    },
                },
            })
        defs.append({
            "type": "function",
            "function": {
                "name": "start_sleep",
                "description": "Go to sleep for a while.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Minutes of sleep, e.g. 480",
                        },
                        "note": {"type": "string", "description": "Optional note"},
                    },
                    "required": ["duration_minutes"],
                },
            },
        })

    # Proactive-only: one-shot view of recent messages across chats.
    # Sorted old->new by chat freshness, messages NOT truncated.
    if include_proactive and peek_enabled and state in ("rest", "working"):
        try:
            from Mila_observatory.runtime import runtime_config as _rc
            _default_peek = int(_rc.get("proactive.default_peek_limit", 10) or 10)
        except Exception:
            _default_peek = 10
        defs.append({
            "type": "function",
            "function": {
                "name": "peek_recent_chats",
                "description": (
                    "Посмотреть последние сообщения из каждого чата одновременно, "
                    "чтобы решить кому написать. Вернёт чаты от старых к свежим "
                    "(свежие внизу). Сообщения не обрезаются."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit_per_chat": {
                            "type": "integer",
                            "description": (
                                f"Сколько последних сообщений взять из каждого чата "
                                f"(по умолчанию {_default_peek}, ты можешь поставить своё)"
                            ),
                        },
                        "chat_aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Необязательно: алиасы конкретных чатов. "
                                "Если пусто — все доступные чаты."
                            ),
                        },
                    },
                },
            },
        })

    return defs


class ToolExecutor:
    def __init__(
        self,
        text_memory,
        embedding_client,
        current_user_id: str,
        chat_id: str,
        seen_memories: set[str] | None = None,
    ):
        self.text_memory = text_memory
        self.emb_client = embedding_client
        self.current_user_id = current_user_id
        self.chat_id = chat_id
        # Shared across every search_memory call in one turn: the model often
        # issues several overlapping queries, and the same fact must not be
        # injected into the context once per query.
        self.seen_memories = seen_memories if seen_memories is not None else set()

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
        elif action_type == "peek_recent_chats":
            return await self._handle_peek_recent_chats(params)
        else:
            return {
                "type": action_type,
                "status": "unknown",
                "error": f"Unknown action: {action_type}"
            }

    async def _handle_send_message(self, params: Dict) -> Dict:
        text = params.get("text", "")
        chat_id, error = self._resolve_target(params, allow_default=True)
        if error:
            return {"type": "send_message", "status": "error", "error": error}
        return {
            "type": "send_message",
            "text": text,
            "chat_id": chat_id,
            "status": "ready"
        }

    def _resolve_target(self, params: Dict, allow_default: bool = False) -> tuple[str | None, str | None]:
        """Resolve an explicit model target, never falling back after an error."""
        from core.db import get_chat_alias, resolve_chat_alias

        supplied = "chat_alias" in params or "chat_id" in params
        if supplied:
            raw = params.get("chat_alias") if "chat_alias" in params else params.get("chat_id")
            chat_id = resolve_chat_alias(raw) if isinstance(raw, str) else None
            if not chat_id:
                return None, "Неизвестный или пустой алиас чата. Выбери алиас из уведомления или списка чатов."
            return chat_id, None
        if allow_default and self.chat_id not in (None, "", "proactive", "system"):
            if get_chat_alias(self.chat_id):
                return str(self.chat_id), None
        return None, "Укажи chat_alias из уведомления или списка чатов."

    @staticmethod
    def _format_memory_context(results: list[dict]) -> str:
        lines = ["[Search results in memory:]"]
        for r in results[:5]:
            preview = (r.get("content") or "")[:200]
            score = r.get("score")
            if isinstance(score, (int, float)):
                lines.append(f"- {preview} (relevance: {score:.2f})")
            else:
                lines.append(f"- {preview}")
        return "\n".join(lines)

    @staticmethod
    def _memory_keys(mem: dict) -> list[str]:
        """Identity keys for a memory row: id and normalized content.

        Both are needed: the same row can come back from several queries, and
        the extractor sometimes stores the same fact under different ids.
        """
        keys: list[str] = []
        mem_id = str(mem.get("mem_id") or "").strip()
        if mem_id:
            keys.append(f"id:{mem_id}")
        content = " ".join(str(mem.get("content") or "").split()).casefold()
        if content:
            keys.append(f"text:{content}")
        return keys

    def _only_new(self, results: list[dict], limit: int = 5) -> list[dict]:
        """Drop facts already surfaced earlier in this turn.

        The model may call search_memory several times with overlapping
        queries; without this filter every call re-injects the same facts
        into the context and bloats the prompt.

        Only the first ``limit`` fresh rows are actually injected by
        ``_format_memory_context``, so only those are marked as seen —
        otherwise a sixth hit would be suppressed despite never being shown.
        """
        fresh: list[dict] = []
        for mem in results:
            keys = self._memory_keys(mem)
            if keys and any(k in self.seen_memories for k in keys):
                continue
            if len(fresh) < limit:
                self.seen_memories.update(keys)
            fresh.append(mem)
        return fresh

    @staticmethod
    def _duplicate_context() -> str:
        return "[No new facts: everything found is already shown above]"

    def _sqlite_fallback(self, query: str, reason: str = "") -> Dict:
        """Keyword search in SQLite when embeddings/FAISS are unavailable (M9).

        Better to remember a fact approximately than to report "nothing
        found" just because the embedding backend is down.
        """
        from core.db import search_memories_like

        memories = search_memories_like(self.current_user_id, query, limit=5)
        if not memories:
            # Legacy rows with NULL user_id (same rule as the FAISS path).
            memories = search_memories_like(None, query, limit=5)
        raw_results = [{**m, "score": None} for m in memories]
        results = self._only_new(raw_results)
        if results:
            context = self._format_memory_context(results)
        elif raw_results:
            context = self._duplicate_context()
        else:
            context = "[Nothing found in memory]"
        print(f"[ToolExecutor] Search fallback=true, source=sqlite: '{query}' "
              f"→ {len(results)} new, {len(raw_results) - len(results)} skipped ({reason})")
        return {
            "type": "search_memory",
            "query": query,
            "results": results,
            "context_addon": context,
            "status": "searched",
            "source": "sqlite_fallback",
            "fallback": True,
            "error": reason or None,
        }

    async def _handle_search_memory(self, params: Dict) -> Dict:
        query = (params.get("query") or "").strip()
        if not query:
            return {
                "type": "search_memory",
                "query": query,
                "results": None,
                "context_addon": "[Empty memory search query]",
                "status": "error",
                "error": "empty query",
            }

        from core.db import search_memories_by_faiss_ids

        try:
            # Bound the wait: a dead embedding backend must not stall the turn
            # for minutes before the SQLite fallback kicks in.
            vec = (await asyncio.wait_for(
                self.emb_client.embed([query]), timeout=15.0))[0]

            # search() reloads the index if the Observatory wrote it (M3).
            distances, indices = self.text_memory.search(vec, k=10)
            valid_indices = [int(idx) for idx in indices if idx != -1]
            dist_by_index = {int(idx): float(d)
                             for idx, d in zip(indices, distances) if idx != -1}

            results: list[dict] = []
            if valid_indices:
                # Per-user isolation: never surface another user's facts.
                # Legacy rows with NULL user_id are visible to everyone only
                # when no per-user rows matched (backward compat).
                memories = search_memories_by_faiss_ids(valid_indices, user_id=self.current_user_id)
                if not memories:
                    memories = [m for m in search_memories_by_faiss_ids(valid_indices)
                                if not m.get("user_id")]

                # Preserve FAISS distance order; the DB result set is unordered.
                by_faiss = {int(m["faiss_id"]): m for m in memories
                            if m.get("faiss_id") is not None}
                ordered = [by_faiss[i] for i in valid_indices if i in by_faiss]

                if ordered:
                    try:
                        from core.llm.reranker import RerankerClient
                        reranker = RerankerClient()
                        documents = [m.get("content", "") for m in ordered]
                        reranked = await reranker.rerank(query, documents)
                        seen = set()
                        for doc, score in reranked:
                            for mem in ordered:
                                if mem.get("content") == doc and mem.get("mem_id") not in seen:
                                    results.append({"content": doc, "score": score, **mem})
                                    seen.add(mem.get("mem_id"))
                                    break
                    except Exception as e:
                        # Reranker is optional (model may not be installed);
                        # FAISS order is still a valid answer.
                        print(f"[ToolExecutor] Reranker unavailable ({e}); using FAISS order")
                        results = [
                            {
                                "content": m.get("content", ""),
                                "score": float(1.0 / (1.0 + max(0.0, float(d)))),
                                **m,
                            }
                            for m in ordered
                            for d in (dist_by_index.get(int(m["faiss_id"]), 0.0),)
                        ]

            if results:
                raw_count = len(results)
                results = self._only_new(results)
                if results:
                    print(f"[ToolExecutor] Search: '{query}' → "
                          f"{len(results)} new, {raw_count - len(results)} skipped")
                    return {
                        "type": "search_memory",
                        "query": query,
                        "results": results,
                        "context_addon": self._format_memory_context(results),
                        "status": "searched",
                        "source": "faiss",
                        "fallback": False,
                    }
                print(f"[ToolExecutor] Search: '{query}' → "
                      f"all {raw_count} results already in context")
                return {
                    "type": "search_memory",
                    "query": query,
                    "results": [],
                    "context_addon": self._duplicate_context(),
                    "status": "searched",
                    "source": "faiss",
                    "fallback": False,
                }

            # No vector hit (empty index, wrong user, or embeddings down) —
            # SQLite keyword search keeps facts reachable.
            return self._sqlite_fallback(query, "no FAISS results")

        except Exception as e:
            print(f"[ToolExecutor] Search error: {e}")
            return self._sqlite_fallback(query, str(e))

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
        from core.db import get_chat_display_name
        note = params.get("note")
        if not isinstance(note, str) or not note.strip():
            return {"type": "enter_dialog", "status": "error",
                    "error": "Укажи непустую причину перехода в поле note."}
        note = note.strip()
        chat_id, error = self._resolve_target(params)
        if error:
            return {"type": "enter_dialog", "status": "error",
                    "error": error}
        try:
            user_name = get_chat_display_name(chat_id)
        except Exception:
            user_name = "Без имени"
        presence = enter_dialog(chat_id, user_name, note)
        print(f"[Presence] → dialog with {user_name} (chat {chat_id})")
        return {"type": "enter_dialog", "chat_id": chat_id,
                "user_name": user_name,
                "state": presence.get("state", "dialog"),
                "presence": presence, "status": "switched",
                "journal_event": presence.get("journal_event"), "note": note}

    async def _handle_enter_rest(self, params: Dict) -> Dict:
        from core.presence import enter_rest
        note = str(params.get("note", "") or "")
        presence = enter_rest(note)
        print("[Presence] → rest")
        return {"type": "enter_rest", "state": presence.get("state", "rest"),
                "presence": presence, "status": "switched",
                "journal_event": presence.get("journal_event")}

    async def _handle_enter_working(self, params: Dict) -> Dict:
        from core.presence import enter_working
        note = str(params.get("note", "") or "")
        presence = enter_working(note)
        print(f"[Presence] → working ({note[:60]})")
        return {"type": "enter_working",
                "state": presence.get("state", "working"),
                "presence": presence, "status": "switched",
                "journal_event": presence.get("journal_event")}

    async def _handle_start_sleep(self, params: Dict) -> Dict:
        from core.presence import get_presence
        try:
            duration = int(params.get("duration_minutes", 0) or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            return {"type": "start_sleep", "status": "error",
                    "error": "Missing duration_minutes (how long to sleep)"}
        duration = max(1, min(duration, 12 * 60))
        note = str(params.get("note", "") or "")
        prev = get_presence()
        # start_sleep switches presence itself. Calling enter_sleep_note here
        # would make the journal lose the real previous state.
        try:
            from core.sleep.manager import start_sleep
            result = await start_sleep(duration_minutes=duration,
                                       triggered_by="mila_tool", note=note)
        except Exception as e:
            result = {"success": False, "message": str(e)}
        if not result.get("success"):
            # Refused (e.g. forced_awake) → don't leave presence stuck
            # in sleep while Mila is actually awake; restore previous state.
            # journal=False: a refused sleep is not a real transition.
            try:
                from core.sleep.manager import is_sleeping as _is_sleeping
                if not _is_sleeping():
                    if prev.get("state") == "dialog" and prev.get("focus_chat_id"):
                        from core.presence import enter_dialog as _ed
                        _ed(str(prev["focus_chat_id"]),
                            prev.get("focus_user_name"), "sleep refused",
                            journal=False)
                    elif prev.get("state") == "working":
                        from core.presence import enter_working as _ew
                        _ew(prev.get("note", ""), journal=False)
                    else:
                        from core.presence import enter_rest as _er
                        _er("sleep refused", journal=False)
            except Exception:
                pass
            return {"type": "start_sleep", "status": "error",
                    "error": result.get("message", "sleep refused")}
        print(f"[Presence] → sleep for {duration} min")
        return {"type": "start_sleep", "duration_minutes": duration,
                "sleep_result": result, "status": "sleeping"}

    async def _handle_peek_recent_chats(self, params: Dict) -> Dict:
        """One-shot view of recent messages across chats, old->new.

        No truncation of message texts (per spec). Chats ordered by last
        activity ASC: stale in the middle of context, fresh at the bottom.
        """
        try:
            limit = int(params.get("limit_per_chat", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            try:
                from Mila_observatory.runtime import runtime_config as _rc
                limit = int(_rc.get("proactive.default_peek_limit", 10) or 10)
            except Exception:
                limit = 10
        limit = max(1, min(limit, 50))
        wanted = params.get("chat_aliases", params.get("chat_ids")) or []
        if isinstance(wanted, str):
            wanted = [wanted]
        if not isinstance(wanted, list):
            return {"type": "peek_recent_chats", "status": "error", "error": "chat_aliases должен быть списком."}
        if wanted:
            from core.db import resolve_chat_alias
            resolved = [resolve_chat_alias(x) if isinstance(x, str) else None for x in wanted]
            if not all(resolved):
                return {"type": "peek_recent_chats", "status": "error",
                        "error": "Неизвестный алиас чата. Выбери алиасы из списка чатов."}
            wanted = list(dict.fromkeys(resolved))

        try:
            from core.db import (
                get_chats_by_last_activity,
                get_recent_chunks,
                get_user_display_name,
                get_chat_alias,
                get_chat_display_name,
            )
            try:
                from Mila_observatory.runtime import runtime_config as _rc
                max_chats = int(_rc.get("proactive.max_chats", 20) or 20)
            except Exception:
                max_chats = 20

            if wanted:
                # Keep requested chats, but still order old->new by last activity.
                all_chats = get_chats_by_last_activity(limit=1000000)
                by_id = {c["chat_id"]: c for c in all_chats}
                chats = [by_id[cid] for cid in wanted if cid in by_id]
                # Requested but unknown (no chunks yet) — still show stub.
                known = set(by_id)
                for cid in wanted:
                    if cid not in known:
                        try:
                            nm = get_chat_display_name(cid)
                        except Exception:
                            nm = "Без имени"
                        chats.append({"chat_id": cid, "chat_name": nm,
                                      "last_at": "", "count": 0})
            else:
                chats = get_chats_by_last_activity(limit=max_chats)

            blocks: list[str] = []
            picked: list[dict] = []
            for ch in chats:
                cid = str(ch["chat_id"])
                alias = get_chat_alias(cid)
                if not alias:
                    continue
                cname = ch.get("chat_name") or "Без имени"
                try:
                    chunks = get_recent_chunks(cid, limit=limit)
                except Exception:
                    chunks = []
                msgs: list[str] = []
                for r in chunks or []:
                    ts = (r.get("time_start") or "")[:16].replace("T", " ")
                    uid = str(r.get("user_id") or "")
                    try:
                        who = get_user_display_name(uid) if uid else cname
                    except Exception:
                        who = cname
                    text = (r.get("content") or "").strip()
                    if not text:
                        continue
                    # No truncation — full text.
                    msgs.append(f"[{ts}] {who}: {text}" if ts else f"{who}: {text}")
                picked.append({"chat_id": cid, "chat_name": cname,
                               "last_at": ch.get("last_at") or "",
                               "messages": msgs})
                if msgs:
                    body = "\n".join(msgs)
                else:
                    body = "(пока пусто)"
                last = (ch.get("last_at") or "")[:16].replace("T", " ")
                header = f"Чат «{cname}» (алиас: {alias})"
                if last:
                    header += f" — последнее сообщение {last}:"
                else:
                    header += ":"
                blocks.append(f"{header}\n{body}")

            context_addon = "\n\n".join(blocks) if blocks else "[нет доступных чатов]"
            print(f"[ToolExecutor] peek_recent: {len(picked)} чатов x до {limit}")
            return {
                "type": "peek_recent_chats",
                "status": "peeked",
                "limit_per_chat": limit,
                "chats": picked,
                "context_addon": context_addon,
            }
        except Exception as e:
            return {"type": "peek_recent_chats", "status": "error",
                    "error": str(e)}

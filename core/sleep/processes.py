"""
core/sleep/processes.py — The three parallel sleep processes.
"""

import asyncio
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from config import settings, get_night_llm_api_key
from core.llm.client import LLMClient
from core.db import get_conn as get_mila_conn
from core.diary.manager import get_diary_markdown, get_diary_entry_count
from core.memory.eternal import (
    get_all_chunks_markdown, get_chunks_metadata, get_all_active_chunks,
    add_chunk, update_chunk, deactivate_chunk
)
from core.context.buffer import (
    get_context, peek_oldest_messages_by_tokens, compress_messages_into_summary,
    format_created_at_local,
)
from core.llm.summary_guard import SUMMARY_LANGUAGE_RULES, contains_cjk
from core.sleep.state import (
    SleepState, SleepStage, set_stage, stage_operation, get_current_state,
    get_active_dialogs_detail, log_dialog_summary, get_state,
    update_staged_payload,
)
from core.sleep.tools import (
    get_diary_analysis_tools, get_diary_analysis_tool_names,
    get_dialog_summarization_tools, get_dialog_summarization_tool_names,
    get_user_fact_extraction_tools, get_user_fact_extraction_tool_names,
    get_verification_tools, get_verification_tool_names
)


MAX_GENERATION_ITERATIONS = 3
MAX_VERIFICATION_RETRIES = 3
SUMMARIZE_MIN_MESSAGES = 10


def _contains_cjk(text: str) -> bool:
    """True when ``text`` contains CJK/Kana/Hangul characters (S10)."""
    return contains_cjk(text)


def _tool_calls_have_cjk_summary(tool_calls) -> bool:
    """True when a ``save_dialog_summary`` call carries CJK text."""
    for tc in tool_calls or []:
        try:
            if tc.function.name != "save_dialog_summary":
                continue
            args = json.loads(tc.function.arguments or "{}")
        except (AttributeError, TypeError, json.JSONDecodeError):
            continue
        if _contains_cjk(str(args.get("summary_text") or "")):
            return True
    return False


def _quarantine_dir(session_id: str) -> str:
    """Per-session quarantine dir, derived from a *validated* session id.

    m4: the id ends up in a filesystem path. Accept only UUIDs; anything else is
    sanitized into a deterministic token so ``..`` or absolute paths can never
    escape ``MEDIA_DIR/quarantine``.
    """
    raw = str(session_id or "")
    try:
        token = str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        token = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64] or "unknown"
        print(f"  [Sleep] WARNING: non-UUID session id {raw[:16]!r} used for quarantine")
    return os.path.join(str(settings.MEDIA_DIR), "quarantine", token)


def snapshot_media_for_quarantine(session_id: str, chunk_ids: List[str]) -> List[Dict]:
    """Precompute quarantine moves + DB refs for media of summarized messages.

    Must run BEFORE ``detach_media_by_chunk_ids`` and before the files move
    (S13): the staged payload then describes the undo both for the DB refs and
    for the bytes, so rollback applies it idempotently at any crash point.
    """
    ids = [c for c in (chunk_ids or []) if c]
    if not ids:
        return []
    from core.db import get_media_paths_for_chunks, get_media_log_rows_for_paths

    try:
        rows = get_media_paths_for_chunks(ids)
    except Exception as e:
        print(f"  [Sleep] media snapshot failed: {e}")
        return []
    if not rows:
        return []

    all_paths: List[str] = []
    for row in rows:
        path = row.get("media_path") or ""
        if not path:
            continue
        all_paths.append(path)
        twin = os.path.splitext(path)[0] + ".wav"
        if twin != path:
            all_paths.append(twin)
    try:
        media_logs = get_media_log_rows_for_paths(all_paths)
    except Exception:
        media_logs = []

    qdir = _quarantine_dir(session_id)
    entries: List[Dict] = []
    for idx, row in enumerate(rows):
        path = row.get("media_path") or ""
        if not path:
            continue
        files = [{
            "path": path,
            "quarantine": os.path.join(qdir, f"{idx}_{os.path.basename(path)}"),
        }]
        twin = os.path.splitext(path)[0] + ".wav"
        if twin != path:
            # Converted twin belongs to the same audio and dies with it.
            files.append({
                "path": twin,
                "quarantine": os.path.join(qdir, f"{idx}_{os.path.basename(twin)}"),
            })
        entry_paths = {f["path"] for f in files}
        entries.append({
            "chunk_id": row.get("chunk_id"),
            "media_path": path,
            "files": files,
            "media_logs": [
                {"media_id": m.get("media_id"), "file_path": m.get("file_path")}
                for m in media_logs
                if (m.get("file_path") or "") in entry_paths
            ],
        })
    return entries


def move_media_to_quarantine(entries: List[Dict]) -> Tuple[int, int]:
    """Move snapped files into ``data/media/quarantine/{session_id}/``.

    Bytes are never unlinked before commit (S13). Returns ``(moved, failed)``.
    """
    moved = failed = 0
    for entry in entries or []:
        for f in entry.get("files") or []:
            src, dst = f.get("path"), f.get("quarantine")
            if not src or not dst or not os.path.isfile(src):
                continue
            try:
                # m4: quarantine holds user media — keep it owner-only.
                os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
                os.replace(src, dst)
                moved += 1
            except OSError as e:
                failed += 1
                print(f"  [Sleep] media quarantine failed for {src}: {e}")
    return moved, failed


def restore_media_from_quarantine(entries: List[Dict]) -> Tuple[int, int]:
    """Move quarantined files back to their original paths (rollback, S13)."""
    restored = failed = 0
    for entry in entries or []:
        for f in entry.get("files") or []:
            src, dst = f.get("quarantine"), f.get("path")
            if not src or not dst or not os.path.isfile(src):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
                restored += 1
            except OSError as e:
                failed += 1
                print(f"  [Rollback] media restore failed for {dst}: {e}")
    return restored, failed


def purge_media_quarantine(session_id: str) -> int:
    """Drop this session's quarantine dir (after commit / complete rollback)."""
    if not session_id:
        return 0
    path = _quarantine_dir(session_id)
    if not os.path.isdir(path):
        return 0
    shutil.rmtree(path, ignore_errors=True)
    print(f"  [Sleep] quarantine purged for {str(session_id)[:8]}...")
    return 1


def _get_night_tool_choice() -> str:
    """Tool choice for night workers. Some models fail with 'required',
    so it is configurable via Observatory (/model page)."""
    try:
        from Mila_observatory.runtime import runtime_config
        return runtime_config.get("night_llm.tool_choice", "required") or "required"
    except Exception:
        return "required"


def _create_night_llm() -> LLMClient:
    try:
        from Mila_observatory.runtime import runtime_config
        night_config = runtime_config.get("night_llm", {})
        model = night_config.get("model")
        api_base = night_config.get("api_base")
        temperature = night_config.get("temperature", 0.3)
        max_tokens = night_config.get("max_tokens", 4096)
        reasoning_effort = night_config.get("reasoning_effort", "none")
        extra_body = night_config.get("extra_body")
    except ImportError:
        model = settings.MAIN_LLM_MODEL
        api_base = settings.LLM_API_BASE
        temperature = 0.3
        max_tokens = 4096
        reasoning_effort = "none"
        extra_body = None

    return LLMClient(
        model=model,
        api_base=api_base,
        api_key=get_night_llm_api_key(),
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )


def _create_verifier_llm() -> LLMClient:
    try:
        from Mila_observatory.runtime import runtime_config
        night_config = runtime_config.get("night_llm", {})
        model = night_config.get("model")
        api_base = night_config.get("api_base")
        reasoning_effort = night_config.get("reasoning_effort", "none")
        extra_body = night_config.get("extra_body")
    except ImportError:
        model = settings.MAIN_LLM_MODEL
        api_base = settings.LLM_API_BASE
        reasoning_effort = "none"
        extra_body = None

    return LLMClient(
        model=model,
        api_base=api_base,
        api_key=get_night_llm_api_key(),
        temperature=0.1,
        max_tokens=2048,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
    )


def resolve_fact_user_id(
    raw: Any, known_user_ids: Optional[set[str]]
) -> tuple[Optional[str], Optional[str]]:
    """Map an LLM-provided user id to a full id seen in this dialog.

    Returns ``(user_id, error)``. The model sometimes copies an 8-char prefix
    from the prompt ("65660908...") or hallucinates an id. We accept a full id
    or a unique prefix, but never create a new user row from a truncated one.
    """
    candidate = str(raw or "").strip().strip("[](){}<> \t")
    candidate = candidate.rstrip(".…").strip()
    if not candidate:
        return None, "empty user_id"

    known = {str(uid) for uid in (known_user_ids or set()) if uid}
    if not known:
        return None, "no dialog participants are known"
    if candidate in known:
        return candidate, None

    matches = sorted(uid for uid in known if uid.startswith(candidate))
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, (
            f"user_id {candidate!r} is ambiguous ({', '.join(matches)}); "
            "use the full id"
        )
    return None, (
        f"user_id {candidate!r} is not a participant in this dialog; "
        f"valid ids: {', '.join(sorted(known))}"
    )


class StagingExecutor:
    def __init__(
        self,
        session_id: str,
        process_type: str,
        known_user_ids: Optional[set[str]] = None,
        default_source_chat_id: Optional[str] = None,
        expected_chat_id: Optional[str] = None,
    ):
        self.session_id = session_id
        self.process_type = process_type
        self.staged_ids: List[int] = []
        self.skipped = False
        # Fact extraction gets the full ids actually present in this dialog.
        # Unknown/truncated ids are rejected instead of creating ghost users.
        self.known_user_ids = {str(uid) for uid in (known_user_ids or set()) if uid}
        self.default_source_chat_id = str(default_source_chat_id or "") or None
        # M3: a summarization executor is bound to exactly one dialog. The model
        # can hallucinate another chat_id and otherwise compact an unrelated
        # conversation (its messages really do get deleted). ``None`` keeps the
        # old permissive behavior for non-summary process types.
        self.expected_chat_id = str(expected_chat_id or "") or None

    async def execute_tool_call(self, tool_call) -> Dict[str, Any]:
        import json

        try:
            name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, AttributeError) as e:
            return {
                "role": "system",
                "content": f"Error parsing tool call: {e}"
            }

        if name == "add_memory_chunk":
            return await self._stage_add_chunk(arguments)
        elif name == "update_memory_chunk":
            return await self._stage_update_chunk(arguments)
        elif name == "delete_memory_chunk":
            return await self._stage_delete_chunk(arguments)

        elif name == "save_dialog_summary":
            return await self._stage_dialog_summary(arguments)
        elif name == "skip_dialog":
            self.skipped = True
            return {
                "role": "system",
                "content": f"Dialog skipped: {arguments.get('reason', 'No reason')}"
            }

        elif name == "add_user_fact":
            return await self._stage_user_fact(arguments)
        elif name == "skip_user_fact_extraction":
            self.skipped = True
            return {
                "role": "system",
                "content": f"Fact extraction skipped: {arguments.get('reason', 'No reason')}"
            }

        elif name == "approve_changes":
            return {
                "role": "system",
                "content": f"✅ Changes approved. Notes: {arguments.get('notes', '')}",
                "_approved": True
            }
        elif name == "reject_changes":
            return {
                "role": "system",
                "content": f"❌ Changes rejected. Reason: {arguments.get('reason', '')}. "
                           f"Hint: {arguments.get('correction_hint', '')}",
                "_rejected": True,
                "_reason": arguments.get('reason', ''),
                "_hint": arguments.get('correction_hint', '')
            }

        return {
            "role": "system",
            "content": f"Unknown tool: {name}"
        }

    async def _stage_add_chunk(self, args: Dict) -> Dict:
        # S16: pre-generate the id and stage the undo record first. A crash
        # between staging and insert then leaves a no-op rollback instead of an
        # untracked chunk.
        chunk_id = str(uuid.uuid4())
        payload = {
            "chunk_id": chunk_id,
            "title": args["title"],
            "content_md": args["content_md"],
            "importance": args.get("importance", 0.5),
        }
        sid = stage_operation(
            session_id=self.session_id,
            operation_type="insert",
            target_table="mila_memory_chunks",
            target_id=chunk_id,
            payload=payload,
        )
        self.staged_ids.append(sid)

        chunk_id = add_chunk(
            title=args["title"],
            content_md=args["content_md"],
            importance=args.get("importance", 0.5),
            chunk_id=chunk_id,
        )

        return {
            "role": "system",
            "content": f"✅ Added memory chunk '{args['title']}' (id: {chunk_id[:8]}...)"
        }

    async def _stage_update_chunk(self, args: Dict) -> Dict:
        chunk_id = args["chunk_id"]
        success = update_chunk(
            chunk_id=chunk_id,
            title=args.get("title"),
            content_md=args.get("content_md"),
            importance=args.get("importance")
        )

        if success:
            sid = stage_operation(
                session_id=self.session_id,
                operation_type="update",
                target_table="mila_memory_chunks",
                target_id=chunk_id,
                payload={
                    "chunk_id": chunk_id,
                    "title": args.get("title"),
                    "content_md": args.get("content_md"),
                    "importance": args.get("importance")
                }
            )
            self.staged_ids.append(sid)
            return {
                "role": "system",
                "content": f"✅ Updated memory chunk {chunk_id[:8]}..."
            }
        else:
            return {
                "role": "system",
                "content": f"❌ Chunk {chunk_id[:8]}... not found or inactive"
            }

    async def _stage_delete_chunk(self, args: Dict) -> Dict:
        chunk_id = args["chunk_id"]

        from core.memory.eternal import get_chunk
        chunk = get_chunk(chunk_id)
        if not chunk:
            return {
                "role": "system",
                "content": f"❌ Chunk {chunk_id[:8]}... not found"
            }

        deactivate_chunk(chunk_id)

        sid = stage_operation(
            session_id=self.session_id,
            operation_type="delete",
            target_table="mila_memory_chunks",
            target_id=chunk_id,
            payload={
                "chunk_id": chunk_id,
                "title": chunk.get("title"),
                "reason": args.get("reason", "")
            }
        )
        self.staged_ids.append(sid)

        return {
            "role": "system",
            "content": f"✅ Deactivated memory chunk {chunk_id[:8]}... Reason: {args.get('reason', '')}"
        }

    async def _stage_dialog_summary(self, args: Dict) -> Dict:
        chat_id = str(args.get("chat_id") or "").strip()
        summary_text = str(args.get("summary_text") or "").strip()
        message_count = args.get("message_count", 0)

        # M3: never compact a chat this executor was not created for. The model
        # can copy a chat_id from the prompt of another dialog; honoring it
        # would delete real messages of an unrelated conversation.
        if self.expected_chat_id and chat_id != self.expected_chat_id:
            print(f"  [Sleep] summary chat mismatch: model={chat_id[:12]}... "
                  f"expected={self.expected_chat_id[:12]}... — rejected")
            return {
                "role": "system",
                "content": (
                    f"ERROR: chat_id {chat_id!r} is not the dialog under review. "
                    f"Use chat_id {self.expected_chat_id!r} (or call skip_dialog)."
                ),
            }
        if not chat_id:
            return {
                "role": "system",
                "content": "ERROR: missing chat_id; call save_dialog_summary again."
            }
        if not summary_text:
            return {
                "role": "system",
                "content": "ERROR: empty summary; call save_dialog_summary again."
            }
        if _contains_cjk(summary_text):
            return {
                "role": "system",
                "content": "ERROR: the summary contains Chinese characters. "
                           "Rewrite it in Russian only and call save_dialog_summary again."
            }

        # S8/S9: peek first — the messages are removed only together with the
        # summary insert (one transaction inside the context buffer), and the
        # peeked rows become the undo payload for rollback (S2).
        to_remove = peek_oldest_messages_by_tokens(chat_id, 50000)

        payload = {
            "chat_id": chat_id,
            "summary_text": summary_text,
            "messages_removed": 0,
            "type": "summary",
            "summary_entry_id": None,
            "deleted_rows": to_remove,
            "media": [],
        }
        # S14: the staging row is written before any destructive step so a
        # crash can never leave a summary or deleted messages without an undo.
        sid = stage_operation(
            session_id=self.session_id,
            operation_type="insert",
            target_table=f"context_buffer.chat_{chat_id}",
            target_id=None,
            payload=payload,
        )
        self.staged_ids.append(sid)

        # S13: snapshot media refs and precompute quarantine targets before
        # detaching anything, so rollback can undo a partially applied step.
        chunk_ids = [str(m.get("source_chunk_id") or "") for m in to_remove]
        chunk_ids = [c for c in chunk_ids if c]
        media_entries = snapshot_media_for_quarantine(self.session_id, chunk_ids)
        if media_entries:
            payload["media"] = media_entries
            self._persist_payload(sid, payload)

        # Mark the destructive step as *started*: if the process dies inside
        # compress, rollback knows the summary may exist even without its id.
        payload["compressing"] = True
        self._persist_payload(sid, payload)

        try:
            summary_id, messages_removed = compress_messages_into_summary(
                chat_id, summary_text, [m["id"] for m in to_remove])
        except Exception:
            # m1: the compress transaction rolled back, so no summary exists.
            # Clearing the flag keeps rollback from text-matching (and possibly
            # deleting) an older committed summary with identical text.
            payload.pop("compressing", None)
            self._persist_payload(sid, payload)
            raise

        payload["summary_entry_id"] = summary_id
        payload["messages_removed"] = messages_removed
        payload.pop("compressing", None)
        self._persist_payload(sid, payload)

        if media_entries:
            try:
                from core.db import detach_media_by_chunk_ids
                detached = detach_media_by_chunk_ids(
                    [e["chunk_id"] for e in media_entries if e.get("chunk_id")])
                moved, failed = move_media_to_quarantine(media_entries)
                print(f"  [Sleep] media quarantined for {chat_id[:12]}...: "
                      f"{len(detached)} chunk(s), {moved} file(s) moved, {failed} failed")
            except Exception as e:
                print(f"  [Sleep] media detach failed for {chat_id[:12]}...: {e}")

        if message_count and messages_removed != message_count:
            print(f"  [Sleep] summary for {chat_id[:12]}...: model reported "
                  f"{message_count} messages, actually removed {messages_removed}")

        log_dialog_summary(self.session_id, chat_id, summary_text, messages_removed)

        return {
            "role": "system",
            "content": f"✅ Summary saved for chat {chat_id[:8]}... ({messages_removed} messages compressed)"
        }

    def _persist_payload(self, staging_id: int, payload: Dict) -> bool:
        """Rewrite the staged undo payload; a silent failure would break rollback."""
        ok = update_staged_payload(staging_id, payload)
        if not ok:
            print(f"  [Sleep] WARNING: staging payload {staging_id} not updated "
                  f"(row missing or already applied)")
        return ok

    async def _stage_user_fact(self, args: Dict) -> Dict:
        from core.db import ensure_user_exists
        from core.memory.indexer import get_indexer

        user_id, error = resolve_fact_user_id(
            args.get("user_id"), self.known_user_ids)
        if error:
            return {
                "role": "system",
                "content": (
                    f"ERROR: {error}. Do not truncate ids; call add_user_fact "
                    "again with the exact full user_id from the [user_id] prefix."
                ),
            }

        content = str(args.get("content") or "").strip()
        if not content:
            return {
                "role": "system",
                "content": "ERROR: empty content; call add_user_fact with a non-empty fact.",
            }

        try:
            importance = float(args.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))

        source_chat_id = (
            self.default_source_chat_id
            or str(args.get("source_chat_id") or "").strip()
        )
        if not source_chat_id:
            return {
                "role": "system",
                "content": "ERROR: missing source_chat_id; call add_user_fact again.",
            }

        # Stage the users row before the memory row. Rollback reverses the
        # order, so it deletes the memory first and only then removes a user
        # row that this sleep session created (existing profiles are kept).
        # Read-then-stage-then-insert closes the untracked-insert window (S15):
        # rollback only deletes users without remaining references anyway.
        from core.db import ensure_user_exists, get_user_info
        from core.sleep.state import discard_staged_operation
        staged_user_id = None
        if get_user_info(user_id) is None:
            staged_user_id = stage_operation(
                session_id=self.session_id,
                operation_type="insert",
                target_table="users",
                target_id=user_id,
                payload={"user_id": user_id},
            )
        # m9: the insert return value is the only authoritative "we created it".
        # If a racing writer got there first, drop our staged users row so
        # rollback can never delete a profile this session did not create.
        if ensure_user_exists(user_id) is False and staged_user_id:
            discard_staged_operation(staged_user_id)

        # S15: pre-generate the id and stage the undo record BEFORE inserting.
        # Insert + embed + FAISS in one step; if the embedding backend is down
        # the row stays with faiss_id=NULL and is retried later.
        mem_id = str(uuid.uuid4())
        sid = stage_operation(
            session_id=self.session_id,
            operation_type="insert",
            target_table="memories",
            target_id=mem_id,
            payload={
                "mem_id": mem_id,
                "user_id": user_id,
                "content": content,
                "importance": importance,
                "source_chat_id": source_chat_id
            }
        )
        self.staged_ids.append(sid)

        actual_id, indexed = await get_indexer().add_memory(
            mem_id=mem_id,
            user_id=user_id,
            content=content,
            importance=importance,
            source_chat_id=source_chat_id
        )
        if not indexed:
            print(f"  [Sleep] memory {actual_id[:8]}... queued for reindex")
        if actual_id != mem_id:
            # Non-conforming indexer ignored the pre-generated id; keep the
            # rollback pointed at the row that actually exists.
            sid = stage_operation(
                session_id=self.session_id,
                operation_type="insert",
                target_table="memories",
                target_id=actual_id,
                payload={"mem_id": actual_id, "user_id": user_id},
            )
            self.staged_ids.append(sid)
            mem_id = actual_id

        return {
            "role": "system",
            "content": f"✅ Fact added for user {user_id[:8]}...: {content[:100]}..."
        }


async def process_diary_analysis(session_id: str) -> Tuple[bool, str]:
    print("\n" + "="*60)
    print("  PROCESS 1: Diary Analysis")
    print("="*60)

    set_stage(SleepStage.DIARY_ANALYSIS, progress_pct=10.0)

    llm = _create_night_llm()
    verifier = _create_verifier_llm()
    executor = StagingExecutor(session_id, "diary_analysis")

    diary_md = get_diary_markdown()
    diary_count = get_diary_entry_count()

    if diary_count == 0:
        print("[DiaryAnalysis] No diary entries to analyze")
        return True, "No diary entries"

    eternal_memory_md = get_all_chunks_markdown()
    chunks_metadata = get_chunks_metadata()

    metadata_str = json.dumps(chunks_metadata, ensure_ascii=False, indent=2) if chunks_metadata else "[]"

    system_prompt = f"""You are Mila's night reflection system (Process 1: Diary Analysis).

Your task is to analyze today's diary entries and decide what should be added to Mila's eternal memory.

## Current Eternal Memory

{eternal_memory_md}

## Metadata of Existing Chunks

{metadata_str}

## Today's Diary

{diary_md}

## Instructions

1. Read the diary carefully.
2. Compare with existing eternal memory.
3. Identify events that are GENUINELY IMPORTANT enough for eternal memory:
   - Serious personality changes
   - Extremely important realizations
   - Events that will influence future behavior
   - NOT: everyday events, minor conversations, routine
4. Use the available tools to:
   - Add a new chunk for genuinely new important events
   - Update an existing chunk if it needs revision
   - Delete a chunk if it's no longer relevant
5. You MUST call at least one tool. If you don't, the iteration will be retried.
6. Be selective. Not every diary entry needs to be in eternal memory.
"""

    generation_messages = [{"role": "user", "content": "Analyze today's diary and update eternal memory as needed."}]
    tools = get_diary_analysis_tools()
    tool_names = get_diary_analysis_tool_names()

    generation_success = False
    verification_result = None

    for gen_iter in range(MAX_GENERATION_ITERATIONS):
        print(f"  [Gen] Iteration {gen_iter + 1}/{MAX_GENERATION_ITERATIONS}")

        try:
            response = await llm.generate(
                messages=generation_messages,
                system_prompt=system_prompt,
                tools=tools,
                tool_choice=_get_night_tool_choice(),
                max_tokens=4096,
                temperature=0.3
            )
        except Exception as e:
            print(f"  [Gen] Error: {e}")
            if gen_iter < MAX_GENERATION_ITERATIONS - 1:
                continue
            return False, f"Generation error: {e}"

        tool_calls = response.get("tool_calls")
        if not tool_calls:
            print(f"  [Gen] No tools called (iteration {gen_iter + 1})")
            if gen_iter < MAX_GENERATION_ITERATIONS - 1:
                generation_messages.append({
                    "role": "system",
                    "content": "ERROR: You didn't call any tools. You MUST call at least one tool. "
                               "Re-analyze the diary and make changes to eternal memory."
                })
                continue
            else:
                return False, "No tools called after all iterations"

        all_results = []
        for tc in tool_calls:
            result = await executor.execute_tool_call(tc)
            all_results.append(result)

        for result in all_results:
            generation_messages.append(result)

        generation_messages.append({
            "role": "user",
            "content": "Analysis complete. Now verify the changes."
        })

        generation_success = True
        break

    if not generation_success:
        return False, "Generation phase failed"

    set_stage(SleepStage.VERIFICATION, progress_pct=50.0)

    gen_tool_calls = tool_calls

    verify_prompt = f"""You are the verification system for Mila's diary analysis.

Check the following changes that were made to Mila's eternal memory:

## Changes Made
{json.dumps([{"tool": tc.function.name, "args": json.loads(tc.function.arguments)} for tc in gen_tool_calls] if gen_tool_calls and hasattr(gen_tool_calls[0], 'function') else [], ensure_ascii=False, indent=2)}

## Verification Criteria
1. Are the changes relevant to Mila's own life experience?
2. Are they genuinely important enough for eternal memory?
3. Are the titles and content well-formed in Markdown?
4. Would any of these changes cause contradictions or confusion?

If everything looks correct, call approve_changes.
If there are issues, call reject_changes with a detailed explanation.
"""

    for verify_iter in range(MAX_VERIFICATION_RETRIES):
        print(f"  [Verify] Iteration {verify_iter + 1}/{MAX_VERIFICATION_RETRIES}")

        try:
            response = await verifier.generate(
                messages=[{"role": "user", "content": "Verify the changes made to eternal memory."}],
                system_prompt=verify_prompt,
                tools=get_verification_tools(),
                tool_choice=_get_night_tool_choice(),
                max_tokens=2048,
                temperature=0.1
            )
        except Exception as e:
            print(f"  [Verify] Error: {e}")
            if verify_iter < MAX_VERIFICATION_RETRIES - 1:
                continue
            return False, f"Verification error: {e}"

        tool_calls = response.get("tool_calls")
        if not tool_calls:
            if verify_iter < MAX_VERIFICATION_RETRIES - 1:
                continue
            return False, "Verifier didn't call any tool"

        for tc in tool_calls:
            result = await executor.execute_tool_call(tc)
            if result.get("_approved"):
                print("  ✅ Verification: APPROVED")
                return True, "Diary analysis changes verified and approved"
            elif result.get("_rejected"):
                print(f"  ❌ Verification: REJECTED - {result.get('_reason', '')}")
                if verify_iter < MAX_VERIFICATION_RETRIES - 1:
                    generation_messages.append({
                        "role": "system",
                        "content": f"Verification rejected. Reason: {result.get('_reason', '')}. "
                                   f"Hint: {result.get('_hint', '')}. Please re-analyze and make corrected changes."
                    })
                    return await _retry_generation(llm, generation_messages, system_prompt, tools, 
                                                   executor, MAX_GENERATION_ITERATIONS, session_id, "diary_analysis")
                else:
                    return False, f"Verification rejected: {result.get('_reason', '')}"

    return False, "Verification phase failed"


async def _retry_generation(llm, messages, system_prompt, tools, executor, max_iter, session_id, process_type):
    for gen_iter in range(max_iter):
        print(f"  [Re-gen] Iteration {gen_iter + 1}/{max_iter}")

        try:
            response = await llm.generate(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
                tool_choice=_get_night_tool_choice(),
                max_tokens=4096,
                temperature=0.3
            )
        except Exception as e:
            print(f"  [Re-gen] Error: {e}")
            continue

        tool_calls = response.get("tool_calls")
        if not tool_calls:
            continue

        for tc in tool_calls:
            await executor.execute_tool_call(tc)

        return True, "Changes re-generated after verification rejection"

    return False, "Retry generation failed"


async def process_dialog_summarization(session_id: str) -> Tuple[bool, str]:
    print("\n" + "="*60)
    print("  PROCESS 2: Dialog Summarization")
    print("="*60)

    set_stage(SleepStage.DIALOG_SUMMARIZATION, progress_pct=30.0)

    active_dialogs = get_active_dialogs_detail()

    if not active_dialogs:
        print("[DialogSummarization] No active dialogs")
        return True, "No active dialogs"

    print(f"  Active dialogs: {len(active_dialogs)}")

    llm = _create_night_llm()
    verifier = _create_verifier_llm()

    results = []

    for idx, dialog in enumerate(active_dialogs):
        chat_id = dialog["chat_id"]
        message_count = dialog["message_count"]

        progress = 30.0 + (idx / len(active_dialogs)) * 40.0
        set_stage(SleepStage.DIALOG_SUMMARIZATION, progress_pct=progress)

        print(f"\n  ── Dialog {idx + 1}/{len(active_dialogs)}: {chat_id[:12]}... ({message_count} msgs)")

        if message_count < SUMMARIZE_MIN_MESSAGES:
            print(f"  → Skipped: only {message_count} messages (need {SUMMARIZE_MIN_MESSAGES})")
            results.append({"chat_id": chat_id, "action": "skipped", "reason": "too few messages"})
            continue

        from core.context.buffer import get_context
        context = get_context(chat_id, limit=200)
        # Stored timestamps are UTC (S12); the model reasons about dates in the
        # user's local day, so the prompt gets the converted date.
        messages_text = "\n".join(
            f"[{format_created_at_local(m.get('created_at', ''))[:10]}] {m['content'][:500]}"
            for m in context
            if m['type'] == 'message' and m['content'].strip()
        )

        if not messages_text.strip():
            print("  → Skipped: empty context")
            results.append({"chat_id": chat_id, "action": "skipped", "reason": "empty context"})
            continue

        # M3: bind the executor to this dialog — a hallucinated chat_id must not
        # compact another conversation.
        executor = StagingExecutor(
            session_id, "dialog_summarization", expected_chat_id=chat_id)

        system_prompt = f"""You are Mila's night reflection system (Process 2: Dialog Summarization).

Your task is to analyze dialog messages and create a concise summary.

## Messages from chat {chat_id}

{messages_text[:8000]}

## Instructions

1. Read the dialog messages.
2. If the conversation has enough substance, create a summary using save_dialog_summary
   with chat_id "{chat_id}" — the chat under review, never another one.
3. The summary should be 2-4 sentences covering:
   - Key topics discussed
   - Any decisions or agreements made
   - Emotional context and tone
4. {SUMMARY_LANGUAGE_RULES}
5. If the dialog doesn't have enough meaningful content, use skip_dialog.
6. You MUST call either save_dialog_summary or skip_dialog.
"""

        gen_messages = [{"role": "user", "content": f"Analyze dialog {chat_id[:12]}... and create summary or skip."}]
        tools = get_dialog_summarization_tools()

        generation_success = False
        cjk_retried = False
        cjk_skipped = False
        for gen_iter in range(MAX_GENERATION_ITERATIONS):
            try:
                response = await llm.generate(
                    messages=gen_messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    tool_choice=_get_night_tool_choice(),
                    max_tokens=2048,
                    temperature=0.3
                )
            except Exception as e:
                print(f"  [Gen] Error: {e}")
                continue

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                gen_messages.append({
                    "role": "system",
                    "content": "ERROR: You must call a tool. Either save_dialog_summary or skip_dialog."
                })
                continue

            # S10: reject CJK output and ask for a Russian rewrite exactly once.
            if _tool_calls_have_cjk_summary(tool_calls):
                if not cjk_retried:
                    cjk_retried = True
                    print("  [Gen] Summary contained CJK characters — asking for a Russian rewrite")
                    gen_messages.append({
                        "role": "system",
                        "content": "ERROR: the summary contained Chinese characters. "
                                   "Rewrite it in Russian only (no Chinese/English words) "
                                   "and call save_dialog_summary again."
                    })
                    continue
                print(f"  → Skipped {chat_id[:12]}...: summary still contains CJK")
                results.append({"chat_id": chat_id, "action": "skipped",
                                "reason": "summary in Chinese"})
                cjk_skipped = True
                break

            for tc in tool_calls:
                await executor.execute_tool_call(tc)

            generation_success = True
            break

        if cjk_skipped:
            continue

        if not generation_success:
            results.append({"chat_id": chat_id, "action": "error", "reason": "generation failed"})
            continue

        if executor.skipped:
            results.append({"chat_id": chat_id, "action": "skipped", "reason": "LLM decided to skip"})
            continue

        for verify_iter in range(MAX_VERIFICATION_RETRIES):
            verify_prompt = f"""Verify the dialog summary for chat {chat_id}.

Check:
1. Does the summary accurately reflect the conversation?
2. Is it concise (2-4 sentences)?
3. Does it include key topics and emotional context?

Call approve_changes if good, reject_changes if issues found.
"""
            try:
                response = await verifier.generate(
                    messages=[{"role": "user", "content": f"Verify summary for dialog {chat_id[:12]}..."}],
                    system_prompt=verify_prompt,
                    tools=get_verification_tools(),
                    tool_choice=_get_night_tool_choice(),
                    max_tokens=1024,
                    temperature=0.1
                )
            except Exception as e:
                print(f"  [Verify] Error: {e}")
                continue

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                continue

            for tc in tool_calls:
                result = await executor.execute_tool_call(tc)
                if result.get("_approved"):
                    print(f"  ✅ Summary verified for {chat_id[:12]}...")
                    results.append({"chat_id": chat_id, "action": "summarized", "status": "approved"})
                    break
                elif result.get("_rejected"):
                    print(f"  ❌ Summary rejected for {chat_id[:12]}...")
                    results.append({"chat_id": chat_id, "action": "summarized", "status": "rejected"})
                    break
            else:
                continue
            break

    success_count = sum(1 for r in results if r.get("action") == "summarized" and r.get("status") == "approved")
    print(f"\n  Result: {success_count} dialogs summarized, {len(results) - success_count} skipped/failed")

    return True, f"Summarized {success_count}/{len(results)} dialogs"


async def process_user_fact_extraction(session_id: str) -> Tuple[bool, str]:
    print("\n" + "="*60)
    print("  PROCESS 3: User Fact Extraction")
    print("="*60)

    set_stage(SleepStage.USER_FACT_EXTRACTION, progress_pct=70.0)

    active_dialogs = get_active_dialogs_detail()

    if not active_dialogs:
        print("[UserFactExtraction] No active dialogs")
        return True, "No active dialogs"

    print(f"  Active dialogs: {len(active_dialogs)}")

    llm = _create_night_llm()
    verifier = _create_verifier_llm()

    total_facts = 0

    for idx, dialog in enumerate(active_dialogs):
        chat_id = dialog["chat_id"]
        message_count = dialog["message_count"]

        progress = 70.0 + (idx / len(active_dialogs)) * 20.0
        set_stage(SleepStage.USER_FACT_EXTRACTION, progress_pct=progress)

        print(f"\n  ── Dialog {idx + 1}/{len(active_dialogs)}: {chat_id[:12]}... ({message_count} msgs)")

        from core.db import get_recent_chunks
        chunks = get_recent_chunks(chat_id, limit=200)

        messages_text_lines = []
        known_user_ids: set[str] = set()
        for chunk in chunks:
            user_id = str(chunk.get("user_id") or "").strip()
            if user_id:
                known_user_ids.add(user_id)
            content = chunk.get("content", "")
            if content.strip():
                messages_text_lines.append(f"[{user_id or 'unknown'}]: {content[:500]}")

        messages_text = "\n".join(messages_text_lines)

        if not messages_text.strip():
            print("  → Skipped: no messages")
            continue

        executor = StagingExecutor(
            session_id,
            "user_fact_extraction",
            known_user_ids=known_user_ids,
            default_source_chat_id=chat_id,
        )

        system_prompt = f"""You are Mila's night reflection system (Process 3: User Fact Extraction).

Your task is to extract long-term facts about users from dialog messages.

## Messages from chat {chat_id}

{messages_text[:10000]}

## Instructions

1. Read the messages carefully.
2. Extract ONLY long-term facts about users:
   - Hobbies, interests, preferences
   - Life events, work, family
   - Personality traits that are demonstrated
   - Important opinions or beliefs
3. For each fact, use add_user_fact with:
   - The exact full user_id from the [user_id] prefix in messages
     (never truncate, shorten, or invent an id)
   - A specific, factual description
   - An appropriate importance level
4. If no meaningful facts are found, use skip_user_fact_extraction.
5. In group chats, ensure each fact is attributed to the correct user.
6. You MUST call at least one tool.
"""

        gen_messages = [{"role": "user", "content": f"Extract user facts from dialog {chat_id[:12]}..."}]
        tools = get_user_fact_extraction_tools()

        generation_success = False
        for gen_iter in range(MAX_GENERATION_ITERATIONS):
            try:
                response = await llm.generate(
                    messages=gen_messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    tool_choice=_get_night_tool_choice(),
                    max_tokens=4096,
                    temperature=0.3
                )
            except Exception as e:
                print(f"  [Gen] Error: {e}")
                continue

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                gen_messages.append({
                    "role": "system",
                    "content": "ERROR: You must call a tool. Either add_user_fact or skip_user_fact_extraction."
                })
                continue

            for tc in tool_calls:
                result = await executor.execute_tool_call(tc)
                gen_messages.append(result)

            generation_success = True
            break

        if not generation_success:
            print("  → Generation failed")
            continue

        if executor.skipped:
            print("  → No facts found (LLM decided to skip)")
            continue

        facts_added = len(executor.staged_ids)
        total_facts += facts_added

        verified_facts = 0
        for verify_iter in range(MAX_VERIFICATION_RETRIES):
            verify_prompt = f"""Verify the user facts extracted from chat {chat_id}.

Check:
1. Are the facts genuinely long-term (not just conversational trivia)?
2. Are they correctly attributed to the right user?
3. Are they specific and factual (not vague)?
4. Is the importance level appropriate?

Call approve_changes if good, reject_changes if issues found.
"""
            try:
                response = await verifier.generate(
                    messages=[{"role": "user", "content": f"Verify facts extracted from dialog {chat_id[:12]}..."}],
                    system_prompt=verify_prompt,
                    tools=get_verification_tools(),
                    tool_choice=_get_night_tool_choice(),
                    max_tokens=1024,
                    temperature=0.1
                )
            except Exception as e:
                print(f"  [Verify] Error: {e}")
                continue

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                continue

            for tc in tool_calls:
                result = await executor.execute_tool_call(tc)
                if result.get("_approved"):
                    verified_facts = facts_added
                    print(f"  ✅ {facts_added} facts verified for {chat_id[:12]}...")
                    break
                elif result.get("_rejected"):
                    print(f"  ❌ Facts rejected for {chat_id[:12]}...: {result.get('_reason', '')}")
                    if verify_iter < MAX_VERIFICATION_RETRIES - 1:
                        gen_messages.append({
                            "role": "system",
                            "content": f"Verification rejected. Reason: {result.get('_reason', '')}. "
                                       f"Hint: {result.get('_hint', '')}. Please re-extract facts."
                        })
                        for gen_iter in range(MAX_GENERATION_ITERATIONS):
                            try:
                                response = await llm.generate(
                                    messages=gen_messages,
                                    system_prompt=system_prompt,
                                    tools=tools,
                                    tool_choice=_get_night_tool_choice(),
                                    max_tokens=4096,
                                    temperature=0.3
                                )
                            except Exception:
                                continue
                            tc2 = response.get("tool_calls")
                            if not tc2:
                                continue
                            for t in tc2:
                                await executor.execute_tool_call(t)
                            facts_added = len(executor.staged_ids)
                            break
                    break
            else:
                continue
            break

        total_facts += verified_facts
        print(f"  → {verified_facts}/{facts_added} facts verified")

    print(f"\n  Total: {total_facts} facts extracted across {len(active_dialogs)} dialogs")

    return True, f"Extracted {total_facts} user facts"


async def run_all_processes(session_id: str) -> Tuple[bool, str, List[str]]:
    print(f"\n{'='*60}")
    print(f"  Running Sleep Processes (session: {session_id[:8]}...)")
    print(f"{'='*60}\n")

    tasks = [
        process_diary_analysis(session_id),
        process_dialog_summarization(session_id),
        process_user_fact_extraction(session_id),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    process_names = ["Diary Analysis", "Dialog Summarization", "User Fact Extraction"]
    process_results = []
    all_success = True

    for i, (name, result) in enumerate(zip(process_names, results)):
        if isinstance(result, Exception):
            print(f"  ❌ {name}: Error - {result}")
            process_results.append(f"{name}: ERROR - {result}")
            all_success = False
        else:
            success, message = result
            status = "✅" if success else "❌"
            print(f"  {status} {name}: {message}")
            process_results.append(f"{name}: {message}")
            if not success:
                all_success = False

    return all_success, "All processes completed" if all_success else "Some processes failed", process_results

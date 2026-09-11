"""
core/sleep/processes.py — The three parallel sleep processes.
"""

import asyncio
import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from config import settings
from core.llm.client import LLMClient
from core.db import get_conn as get_mila_conn
from core.diary.manager import get_diary_markdown, get_diary_entry_count
from core.memory.eternal import (
    get_all_chunks_markdown, get_chunks_metadata, get_all_active_chunks,
    add_chunk, update_chunk, deactivate_chunk
)
from core.context.buffer import push_summary, get_context, pop_oldest_messages_by_tokens
from core.sleep.state import (
    SleepState, SleepStage, set_stage, stage_operation, get_current_state,
    get_active_dialogs_detail, log_dialog_summary, get_state
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
        api_key = night_config.get("api_key")
        temperature = night_config.get("temperature", 0.3)
        max_tokens = night_config.get("max_tokens", 4096)
    except ImportError:
        model = settings.MAIN_LLM_MODEL
        api_base = settings.LLM_API_BASE
        api_key = settings.LLM_API_KEY
        temperature = 0.3
        max_tokens = 4096

    return LLMClient(
        model=model,
        api_base=api_base,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens
    )


def _create_verifier_llm() -> LLMClient:
    try:
        from Mila_observatory.runtime import runtime_config
        night_config = runtime_config.get("night_llm", {})
        model = night_config.get("model")
        api_base = night_config.get("api_base")
        api_key = night_config.get("api_key")
    except ImportError:
        model = settings.MAIN_LLM_MODEL
        api_base = settings.LLM_API_BASE
        api_key = settings.LLM_API_KEY

    return LLMClient(
        model=model,
        api_base=api_base,
        api_key=api_key,
        temperature=0.1,
        max_tokens=2048
    )


class StagingExecutor:
    def __init__(self, session_id: str, process_type: str):
        self.session_id = session_id
        self.process_type = process_type
        self.staged_ids: List[int] = []
        self.skipped = False

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
        chunk_id = add_chunk(
            title=args["title"],
            content_md=args["content_md"],
            importance=args.get("importance", 0.5)
        )

        sid = stage_operation(
            session_id=self.session_id,
            operation_type="insert",
            target_table="mila_memory_chunks",
            target_id=chunk_id,
            payload={
                "chunk_id": chunk_id,
                "title": args["title"],
                "content_md": args["content_md"],
                "importance": args.get("importance", 0.5)
            }
        )
        self.staged_ids.append(sid)

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
        chat_id = args["chat_id"]
        summary_text = args["summary_text"]
        message_count = args.get("message_count", 0)

        push_summary(chat_id, summary_text)

        removed = pop_oldest_messages_by_tokens(chat_id, 50000)
        messages_removed = len(removed) if removed else message_count

        # Detach bytes of summarized-away media: text placeholders and the
        # summary stay, but files are deleted and chunks.media_path cleared
        # so later turns don't re-attach stale photos/audios as fresh info.
        try:
            chunk_ids = [(m.get("source_chunk_id") or "") for m in (removed or [])]
            chunk_ids = [c for c in chunk_ids if c]
            if chunk_ids:
                from core.db import detach_media_by_chunk_ids
                detached = detach_media_by_chunk_ids(chunk_ids)
                if detached:
                    import os as _os
                    n_files = 0
                    for _p in detached:
                        for _cand in (_p, _os.path.splitext(_p)[0] + ".wav"):
                            try:
                                if _cand and _os.path.isfile(_cand):
                                    _os.unlink(_cand)
                                    n_files += 1
                            except OSError:
                                pass
                    print(f"  [Sleep] media detached for {chat_id[:12]}...: "
                          f"{len(detached)} chunk(s), {n_files} file(s) removed")
        except Exception as e:
            print(f"  [Sleep] media detach failed for {chat_id[:12]}...: {e}")

        log_dialog_summary(self.session_id, chat_id, summary_text, messages_removed)

        sid = stage_operation(
            session_id=self.session_id,
            operation_type="insert",
            target_table=f"context_buffer.chat_{chat_id}",
            target_id=None,
            payload={
                "chat_id": chat_id,
                "summary_text": summary_text,
                "messages_removed": messages_removed,
                "type": "summary"
            }
        )
        self.staged_ids.append(sid)

        return {
            "role": "system",
            "content": f"✅ Summary saved for chat {chat_id[:8]}... ({messages_removed} messages compressed)"
        }

    async def _stage_user_fact(self, args: Dict) -> Dict:
        from core.db import add_memory

        user_id = args["user_id"]
        content = args["content"]
        importance = args.get("importance", 0.5)
        source_chat_id = args.get("source_chat_id", "")

        mem_id = add_memory(
            user_id=user_id,
            content=content,
            importance=importance,
            source_chat_id=source_chat_id
        )

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
        messages_text = "\n".join(
            f"[{m.get('created_at', '')[:10]}] {m['content'][:500]}"
            for m in context
            if m['type'] == 'message' and m['content'].strip()
        )

        if not messages_text.strip():
            print("  → Skipped: empty context")
            results.append({"chat_id": chat_id, "action": "skipped", "reason": "empty context"})
            continue

        executor = StagingExecutor(session_id, "dialog_summarization")

        system_prompt = f"""You are Mila's night reflection system (Process 2: Dialog Summarization).

Your task is to analyze dialog messages and create a concise summary.

## Messages from chat {chat_id}

{messages_text[:8000]}

## Instructions

1. Read the dialog messages.
2. If the conversation has enough substance, create a summary using save_dialog_summary.
3. The summary should be 2-4 sentences covering:
   - Key topics discussed
   - Any decisions or agreements made
   - Emotional context and tone
4. If the dialog doesn't have enough meaningful content, use skip_dialog.
5. You MUST call either save_dialog_summary or skip_dialog.
"""

        gen_messages = [{"role": "user", "content": f"Analyze dialog {chat_id[:12]}... and create summary or skip."}]
        tools = get_dialog_summarization_tools()

        generation_success = False
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

            for tc in tool_calls:
                await executor.execute_tool_call(tc)

            generation_success = True
            break

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
        for chunk in chunks:
            user_id = chunk.get("user_id", "unknown")[:8]
            content = chunk.get("content", "")
            if content.strip():
                messages_text_lines.append(f"[{user_id}...]: {content[:500]}")

        messages_text = "\n".join(messages_text_lines)

        if not messages_text.strip():
            print("  → Skipped: no messages")
            continue

        executor = StagingExecutor(session_id, "user_fact_extraction")

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
   - The correct user_id (from the [user_id...] prefix in messages)
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

# core/bot.py
import asyncio
import json
import re
import random
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import settings
from core.db import (
    ensure_user_exists, ensure_chat_exists, add_chunk, get_recent_chunks, get_user_info
)
from core.context_buffer import (
    push_message, push_summary, get_context, get_context_within_token_limit,
    pop_oldest_messages, pop_oldest_messages_by_tokens,
    get_total_entries, get_total_message_tokens, get_raw_content_for_llm
)
from core.token_counter import count_tokens
from core.embedding_client import EmbeddingClient
from core.memory import VectorMemory
from core.pipeline import ThinkPipeline
from core.llm_client import LLMClient
from core.sleep_manager import (
    recover_from_crash, is_sleeping, is_committing, queue_incoming_message,
    start_auto_sleep_scheduler, get_sleep_status
)


class MilaUserbot:
    def __init__(self):
        self.app = Client(
            "mila_session",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            phone_number=settings.PHONE_NUMBER or None,
            device_model=settings.DEVICE_MODEL,
            system_version=settings.SYSTEM_VERSION,
            app_version=settings.APP_VERSION,
            workdir="."
        )
        self.emb = EmbeddingClient()
        self.text_memory = VectorMemory(settings.FAISS_TEXT_PATH, dim=settings.EMBED_DIM)
        self.pipeline = None
        self.me = None
        # Cheap summarizer for daytime context compression
        self.summarizer_llm = None  # lazy init

    async def start(self):
        # Crash recovery: check if previous sleep was interrupted
        await recover_from_crash()
        
        await self.app.start()
        self.me = await self.app.get_me()
        print(f"Bot started as {self.me.first_name} (@{self.me.username})")
        
        # Initialize pipeline after getting bot identity
        self.pipeline = ThinkPipeline(
            text_memory=self.text_memory,
            embedding_client=self.emb,
            user_id="system",
            chat_id="system",
            my_user_id=str(self.me.id)
        )

        from pyrogram.handlers import MessageHandler

        self.app.add_handler(
            MessageHandler(self.handle_text_message, filters.text & filters.private),
            group=0
        )

        asyncio.create_task(self.periodic_save())
        
        # Start auto-sleep scheduler
        start_auto_sleep_scheduler()

    async def periodic_save(self):
        while True:
            await asyncio.sleep(settings.FAISS_SAVE_INTERVAL_MIN * 60)
            self.text_memory.save()

    async def _ensure_summarizer(self):
        """Lazy init of cheap summarizer LLM for daytime context compression."""
        if self.summarizer_llm is None:
            self.summarizer_llm = LLMClient(
                model=settings.MAIN_LLM_MODEL,  # Use same model but with summarization prompt
                api_base=settings.LLM_API_BASE,
                api_key=settings.LLM_API_KEY
            )

    async def _compress_context(self, chat_id: str):
        """
        If context buffer has too many message tokens, compress the oldest ones into a summary.
        Called BEFORE each new message processing to keep context within limits.
        
        Uses token-based counting: only message-type entries count toward the limit.
        Summaries are excluded from the limit (they're already compressed).
        """
        total_message_tokens = get_total_message_tokens(chat_id)
        max_tokens = settings.CONTEXT_MAX_TOKENS
        chunk_tokens = settings.CONTEXT_SUMMARIZE_CHUNK_TOKENS

        if total_message_tokens <= max_tokens:
            return  # No compression needed

        # Pop the oldest message entries by token count
        oldest_messages = pop_oldest_messages_by_tokens(chat_id, chunk_tokens)
        if not oldest_messages:
            return

        # Format them for summarization
        texts_to_summarize = "\n".join(
            f"[{m.get('created_at', '')[:10]}] {m['content'][:500]}"
            for m in oldest_messages
            if m['content'].strip()
        )

        if not texts_to_summarize.strip():
            return

        # Generate summary using cheap LLM
        await self._ensure_summarizer()
        try:
            summary_prompt = (
                "Сократи следующий диалог до краткой сводки (2-3 предложения). "
                "Сохрани только факты: о чём говорили, ключевые темы. "
                "Не добавляй интерпретаций.\n\n"
                f"{texts_to_summarize}"
            )
            response = await self.summarizer_llm.generate(
                messages=[{"role": "user", "content": summary_prompt}],
                max_tokens=300,
                temperature=0.3,
            )
            summary_text = response.get("thoughts", "").strip()
            if not summary_text:
                summary_text = "[Сводка предыдущего разговора]"
        except Exception as e:
            print(f"[Context Compression Error] {e}")
            summary_text = "[Сводка предыдущего разговора]"

        # Push the summary as a context buffer entry
        push_summary(chat_id, summary_text)
        
        # Log token-based stats
        remaining_tokens = get_total_message_tokens(chat_id)
        print(f"[Context Compressed] {len(oldest_messages)} messages (~{chunk_tokens} tokens) → summary. "
              f"Message tokens remaining: {remaining_tokens}/{max_tokens}. "
              f"Total entries: {get_total_entries(chat_id)}.")

    def _build_histories(self, chat_id: str) -> tuple[list[dict], list[dict]]:
        """
        Build two separate histories from the CONTEXT BUFFER (not raw chunks):
        
        1. user_visible_history — only messages that were actually sent to Telegram
        2. agent_internal_history — IDENTICAL to user_visible_history
        
        The context buffer contains only messages that are part of the active
        context window, plus summaries of older conversation segments.
        
        Uses token-based limit: only messages within CONTEXT_MAX_TOKENS are included.
        Summaries are always included (they're already compressed).
        
        Silent (unsent) assistant entries (content="") are SKIPPED entirely.
        """
        context_entries = get_context_within_token_limit(chat_id, settings.CONTEXT_MAX_TOKENS)
        
        user_visible = []
        agent_internal = []
        
        for entry in context_entries:
            content_raw = entry.get("content", "") or ""
            
            if entry["type"] == "summary":
                # Summary entries are injected as a system-like context block
                summary_block = f"[Сводка предыдущего разговора:]\n{content_raw}"
                user_visible.append({
                    "role": "system",
                    "content": summary_block
                })
                agent_internal.append({
                    "role": "system",
                    "content": summary_block
                })
            else:
                user_visible.append({
                    "role": "user",
                    "content": content_raw
                })
                agent_internal.append({
                    "role": "user",
                    "content": content_raw
                })
        
        return user_visible, agent_internal

    def _resolve_roles_in_histories(self, histories: list[dict], chat_id: str) -> list[dict]:
        """
        Resolve roles in context buffer histories by cross-referencing with chunks.
        The context buffer stores only content without roles.
        We look up recent chunks to determine who said what.
        Fetches enough chunks to cover the token-based context limit.
        """
        # Fetch enough chunks to cover the 30K token limit (~500 messages with ~60 tokens each)
        chunks = get_recent_chunks(chat_id, 500)
        # Build a map: content_hash -> role for recent messages
        content_to_role = {}
        for chunk in chunks:
            role = "assistant" if chunk["user_id"] == str(self.me.id) else "user"
            content = chunk.get("content", "") or ""
            if content:
                content_to_role[content] = role

        resolved = []
        for entry in histories:
            if entry["role"] == "system":
                # Keep summaries as system
                resolved.append(entry)
            else:
                content = entry["content"]
                # Try to find role from chunks
                role = content_to_role.get(content, "user")  # default to user
                resolved.append({"role": role, "content": content})
        
        return resolved

    async def handle_text_message(self, client: Client, message: Message):
        user_id = str(message.from_user.id)
        chat_id = str(message.chat.id)
        
        # Always save to canonical storage
        ensure_user_exists(user_id, message.from_user.first_name or "Unknown")
        ensure_chat_exists(chat_id, message.chat.first_name or "Личный чат")
        chunk_id = add_chunk(chat_id, user_id, message.text, token_count=0, type='raw')

        # Check if Mila is sleeping — queue messages and don't process
        if is_sleeping():
            queue_incoming_message(chat_id, user_id, message.text)
            print(f"[Sleep] Message queued from {user_id[:8]}... in {chat_id[:8]}...")
            return

        # Also push to context buffer (daytime working memory)
        push_message(chat_id, message.text, source_chunk_id=chunk_id)

        # Compress context if needed (before processing)
        await self._compress_context(chat_id)

        # Typing emulation
        stop_typing = asyncio.Event()
        async def typing_loop():
            while not stop_typing.is_set():
                await client.send_chat_action(chat_id, enums.ChatAction.TYPING)
                await asyncio.sleep(4)
        typing_task = asyncio.create_task(typing_loop())

        try:
            # Build histories from context buffer
            raw_user_visible, raw_agent_internal = self._build_histories(chat_id)
            
            # Resolve roles by cross-referencing with chunks
            user_visible_history = self._resolve_roles_in_histories(raw_user_visible, chat_id)
            agent_internal_history = self._resolve_roles_in_histories(raw_agent_internal, chat_id)
            
            # Update pipeline context
            self.pipeline.user_id = user_id
            self.pipeline.chat_id = chat_id
            
            # Process through think pipeline
            result = await self.pipeline.process_message(
                user_message=message.text,
                user_visible_history=user_visible_history,
                agent_internal_history=agent_internal_history,
                user_profile=get_user_info(user_id)
            )
            
            # Get full monologue for DB storage
            full_response = result.get("thoughts", "")
            has_send_action = False
            has_silence_action = False
            
            if result.get("success") and result.get("actions"):
                for action in result["actions"]:
                    if action.get("type") == "send_message":
                        has_send_action = True
                        send_text = action.get("text", "")
                        target_chat_id = action.get("chat_id", chat_id)
                        
                        if send_text:
                            print(f"\n[SENDING TO USER] {send_text[:100]}...")
                            await asyncio.sleep(random.uniform(0.5, 2.0))
                            try:
                                # Send to the chat_id specified by the model
                                await client.send_message(int(target_chat_id), send_text)
                                chunk_id = add_chunk(
                                    target_chat_id, str(self.me.id),
                                    content=send_text,
                                    llm_full_response=full_response or send_text,
                                    token_count=0,
                                    type='raw'
                                )
                                # Save assistant response to context buffer too
                                push_message(target_chat_id, send_text, source_chunk_id=chunk_id)
                                print("[✓] Message sent to Telegram")
                            except Exception as e:
                                print(f"[❌ ERROR] Failed to send: {e}")
                        else:
                            print(f"\n[Silent Mode] AI thought but chose not to respond")
                            if full_response:
                                chunk_id = add_chunk(
                                    chat_id, str(self.me.id),
                                    content="",
                                    llm_full_response=full_response,
                                    token_count=0,
                                    type='raw'
                                )
                    
                    elif action.get("type") == "search_memory":
                        print(f"[Memory] Search executed: '{action.get('query', '')}'")
                    
                    elif action.get("type") == "silence":
                        has_silence_action = True
                        silence_reason = action.get("reason", "")
                        print(f"\n[Silence Mode] AI chose not to respond. Reason: {silence_reason}")
                        if full_response:
                            chunk_id = add_chunk(
                                chat_id, str(self.me.id),
                                content="",
                                llm_full_response=full_response,
                                token_count=0,
                                type='raw'
                            )
                    
                    elif action.get("type") == "write_diary":
                        entry_num = action.get("entry_number", "?")
                        print(f"\n[Diary] Entry #{entry_num} written")
            
            if not has_send_action and full_response and not has_silence_action:
                # LLM wrote thoughts but didn't call send_message or silence.
                thoughts_stripped = full_response.strip()
                if len(thoughts_stripped) > 80:
                    print(f"\n[⚠️ FALLBACK] LLM wrote thoughts without calling send_message — auto-sending...")
                    await asyncio.sleep(random.uniform(0.5, 2.0))
                    try:
                        await message.reply_text(thoughts_stripped)
                        chunk_id = add_chunk(
                            chat_id, str(self.me.id),
                            content=thoughts_stripped,
                            llm_full_response=thoughts_stripped,
                            token_count=0,
                            type='raw'
                        )
                        push_message(chat_id, thoughts_stripped, source_chunk_id=chunk_id)
                        print(f"[✓] Fallback message sent to Telegram")
                    except Exception as e:
                        print(f"[❌ ERROR] Fallback send failed: {e}")
                else:
                    # Genuine silent mode — short internal reflection
                    print(f"\n[Silent Mode] AI thought but chose not to respond")
                    if full_response:
                        add_chunk(
                            chat_id, str(self.me.id),
                            content="",
                            llm_full_response=full_response,
                            token_count=0,
                            type='raw'
                        )
                
            if result.get("error"):
                print(f"\n[Pipeline Error] {result['error']}")

        except Exception:
            import traceback
            print("[❌ CRITICAL ERROR] Unhandled exception:")
            traceback.print_exc()
        finally:
            stop_typing.set()
            typing_task.cancel()

    async def stop(self):
        self.text_memory.save()
        await self.app.stop()
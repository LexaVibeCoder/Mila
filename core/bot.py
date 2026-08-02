# core/bot.py
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from config import settings
from core.db import get_recent_chunks
from core.context.buffer import (
    push_summary, get_context_within_token_limit,
    pop_oldest_messages_by_tokens,
    get_total_entries, get_total_message_tokens
)
from core.llm.embeddings import EmbeddingClient
from core.memory.vector import VectorMemory
from core.pipeline import ThinkPipeline
from core.llm.client import LLMClient
from core.sleep.manager import (
    recover_from_crash,
    start_auto_sleep_scheduler
)
from core.queue import MessageCoordinate


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
        # Per-chat batching / debounce coordinator
        self.messages = MessageCoordinate(self)

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
            ts = entry.get("created_at", "")
            time_prefix = ""
            if ts and entry["type"] != "summary":
                time_prefix = f"[{ts}] "

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
                    "content": f"{time_prefix}{content_raw}"
                })
                agent_internal.append({
                    "role": "user",
                    "content": f"{time_prefix}{content_raw}"
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
        """Thin delegator — the batching/processing lives in self.messages."""
        await self.messages.handle(client, message)

    async def stop(self):
        self.text_memory.save()
        await self.app.stop()
"""
core/offline_processor.py — Mila's Night Pipeline (Offline Memory Processor)

Runs as a standalone script. Flow:
1. Wait until SLEEP_START_HOUR
2. Set sleep flag → bot stops processing messages
3. For each chat with messages:
   a. Get unprocessed chunks (from last checkpoint)
   b. Split into chunks of OFFLINE_CHUNK_SIZE
   c. For each chunk: run Memory Worker LLM → extract facts + episode summary
   d. Create overall day summary for the chat
   e. Replace context buffer with the day summary
4. Wait until SLEEP_END_HOUR
5. Clear sleep flag → bot wakes up
6. Exit

Usage:
    python -m core.offline_processor
    or scheduled via cron/Task Scheduler at SLEEP_START_HOUR
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from core.db import (
    get_all_chat_ids_from_chunks, get_unprocessed_chunks,
    get_last_processed_chunk_id, update_processing_tracker,
    save_episode, save_fact, deduplicate_facts
)
from core.context_buffer import replace_all_with_summary, push_summary, ensure_chat_table
from core.model_sleeper import (
    set_sleep_flag, clear_sleep_flag, should_sleep_now,
    time_until_sleep_end, is_sleeping
)
from core.llm_client import LLMClient


class NightPipeline:
    """Offline memory processor — runs during sleep hours."""

    def __init__(self):
        # Initialize night LLM (can be different/more powerful model)
        night_model = settings.NIGHT_LLM_MODEL or settings.MAIN_LLM_MODEL
        night_api_base = settings.NIGHT_LLM_API_BASE or settings.LLM_API_BASE
        night_api_key = settings.NIGHT_LLM_API_KEY or settings.LLM_API_KEY

        self.llm = LLMClient(
            model=night_model,
            api_base=night_api_base,
            api_key=night_api_key,
            max_tokens=settings.NIGHT_LLM_MAX_TOKENS,
            temperature=settings.NIGHT_LLM_TEMPERATURE
        )
        self.chunk_size = settings.OFFLINE_CHUNK_SIZE
        self.start_hour = settings.SLEEP_START_HOUR
        self.end_hour = settings.SLEEP_END_HOUR

    # ── Memory Worker Prompts ──────────────────────────────────────────

    MEMORY_WORKER_SYSTEM_PROMPT = """You are a precise memory extraction system.
Your ONLY task is to analyze conversation fragments and extract factual information.

RULES:
1. Extract facts ONLY if they are EXPLICITLY stated in the text
2. Do NOT infer, guess, or hallucinate missing information
3. Do NOT generalize beyond the evidence
4. Do NOT add interpretations or judgments
5. Create a concise, factual episode summary

OUTPUT FORMAT:
Return a valid JSON object with the following fields:
{
    "episode_summary": "brief factual summary of this conversation segment (1-3 sentences)",
    "facts": [
        {
            "fact": "specific factual statement exactly as supported by text",
            "confidence": 0.0-1.0,
            "source_message_indices": [0, 1, ...]
        }
    ],
    "keywords": ["keyword1", "keyword2"],
    "topics": ["topic1", "topic2"]
}

IMPORTANT: If no explicit facts can be extracted, return "facts": [].
Output ONLY the JSON, no other text."""

    DAY_SUMMARY_SYSTEM_PROMPT = """Create a concise factual summary of today's conversation.
Combine all episode summaries into ONE coherent summary (3-5 sentences).
Focus on: main topics discussed, key decisions made, user's stated preferences or needs.
Do NOT add interpretations or emotional commentary.
Output ONLY the summary text, nothing else."""

    # ── Core Processing ────────────────────────────────────────────────

    async def run(self):
        """Main entry point: wait for sleep time → process → wait for wake → exit."""
        print(f"\n{'='*60}")
        print(f"  🌙 NIGHT PIPELINE STARTED")
        print(f"  Sleep window: {self.start_hour}:00 — {self.end_hour}:00")
        print(f"{'='*60}")

        # Step 1: Wait until sleep window
        await self._wait_until_sleep_start()

        # Step 2: Set sleep flag (bot stops responding)
        print("\n[Pipeline] Setting sleep flag...")
        set_sleep_flag()
        print("[Pipeline] Bot is now sleeping. Starting memory processing...")

        # Step 3: Process all chats
        try:
            await self._process_all_chats()
        except Exception as e:
            print(f"[Pipeline CRITICAL ERROR] {e}")
            import traceback
            traceback.print_exc()

        # Step 4: Wait until morning
        print(f"\n[Pipeline] Memory processing complete. "
              f"Waiting until {self.end_hour}:00 to wake up...")
        await self._wait_until_morning()

        # Step 5: Clear sleep flag
        print("\n[Pipeline] Morning reached. Clearing sleep flag...")
        clear_sleep_flag()
        print("[Pipeline] Bot is now awake. Night pipeline complete.")
        print(f"{'='*60}")

    async def _wait_until_sleep_start(self):
        """Wait until the sleep window starts."""
        now = datetime.now()
        current_hour = now.hour

        if should_sleep_now(self.start_hour, self.end_hour):
            print(f"[Pipeline] Already within sleep window. Proceeding.")
            return

        # Calculate time until sleep starts
        sleep_start = now.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
        if sleep_start <= now:
            sleep_start = sleep_start.replace(day=sleep_start.day + 1)

        wait_seconds = (sleep_start - now).total_seconds()
        print(f"[Pipeline] Waiting {wait_seconds / 3600:.1f} hours until sleep window...")
        
        # Sleep in 1-minute increments so we can be interrupted
        while (sleep_start - datetime.now()).total_seconds() > 0:
            await asyncio.sleep(60)

        print("[Pipeline] Sleep window reached. Starting processing.")

    async def _wait_until_morning(self):
        """Wait until the sleep window ends (morning)."""
        wait_seconds = time_until_sleep_end(self.start_hour, self.end_hour)
        
        if wait_seconds <= 0:
            print("[Pipeline] Sleep window already ended. Waking up.")
            return

        print(f"[Pipeline] Waiting {wait_seconds / 3600:.1f} hours until wake time...")
        
        # Sleep in 1-minute increments
        while wait_seconds > 0:
            await asyncio.sleep(min(60, wait_seconds))
            wait_seconds = time_until_sleep_end(self.start_hour, self.end_hour)

        print("[Pipeline] Wake time reached.")

    # ── Chat Processing ────────────────────────────────────────────────

    async def _process_all_chats(self):
        """Process all chats that have unprocessed messages."""
        chat_ids = get_all_chat_ids_from_chunks()
        print(f"\n[Pipeline] Found {len(chat_ids)} chats to process.")

        for chat_id in chat_ids:
            print(f"\n{'─'*50}")
            print(f"  Processing chat: {chat_id}")
            print(f"{'─'*50}")
            try:
                await self._process_single_chat(chat_id)
            except Exception as e:
                print(f"  [ERROR] Failed to process chat {chat_id}: {e}")
                import traceback
                traceback.print_exc()
                continue

    async def _process_single_chat(self, chat_id: str):
        """Process unprocessed messages for a single chat."""
        last_processed = get_last_processed_chunk_id(chat_id)
        
        all_episode_summaries = []
        all_facts = []

        while True:
            # Get next batch of unprocessed chunks
            chunks = get_unprocessed_chunks(
                chat_id, after_chunk_id=last_processed, limit=self.chunk_size
            )

            if not chunks:
                print(f"  No more unprocessed chunks.")
                break

            print(f"  Processing {len(chunks)} chunks (after {last_processed or 'start'})...")

            # Process this chunk
            episode_data = await self._process_chunk(chat_id, chunks)
            
            if episode_data:
                all_episode_summaries.append(episode_data["episode_summary"])
                all_facts.extend(episode_data["facts"])

                # Save episode
                first_id = chunks[0]["chunk_id"]
                last_id = chunks[-1]["chunk_id"]
                episode_id = save_episode(
                    chat_id=chat_id,
                    summary=episode_data["episode_summary"],
                    range_start=first_id,
                    range_end=last_id,
                    keywords=episode_data.get("keywords", []),
                    topics=episode_data.get("topics", [])
                )
                print(f"  ✓ Episode saved: {episode_id}")

                # Save facts
                fact_count = 0
                for fact_data in episode_data.get("facts", []):
                    fact_text = fact_data.get("fact", "").strip()
                    if not fact_text:
                        continue
                    
                    # Deduplicate
                    if deduplicate_facts(chat_id, fact_text, threshold=0.85):
                        print(f"  ~ Skipped duplicate fact: {fact_text[:60]}...")
                        continue

                    # Map source_message_indices to actual chunk IDs
                    source_ids = []
                    for idx in fact_data.get("source_message_indices", []):
                        if 0 <= idx < len(chunks):
                            source_ids.append(chunks[idx]["chunk_id"])
                    
                    confidence = fact_data.get("confidence", 0.5)
                    save_fact(
                        chat_id=chat_id,
                        fact_text=fact_text,
                        confidence=confidence,
                        source_message_ids=source_ids
                    )
                    fact_count += 1
                
                print(f"  ✓ {fact_count} new facts saved.")

            # Update tracker
            last_processed = chunks[-1]["chunk_id"]
            update_processing_tracker(chat_id, last_processed)

        # Create overall day summary
        if all_episode_summaries:
            day_summary = await self._create_day_summary(all_episode_summaries)
            print(f"  Day summary created: {day_summary[:100]}...")

            # Replace context buffer with day summary
            replace_all_with_summary(chat_id, day_summary)
            print(f"  ✓ Context buffer replaced with day summary.")
        else:
            print(f"  No episodes to summarize for this chat.")

        print(f"  Done. Episodes: {len(all_episode_summaries)}, Facts: {len(all_facts)}")

    # ── Chunk Processing ───────────────────────────────────────────────

    async def _process_chunk(self, chat_id: str, chunks: List[Dict]) -> Optional[Dict]:
        """
        Process a single chunk of messages through the Memory Worker LLM.
        
        Returns structured JSON with episode_summary, facts, keywords, topics.
        """
        # Format messages for the LLM
        formatted_messages = []
        for i, chunk in enumerate(chunks):
            role = "assistant" if chunk.get("user_id") != chat_id else "user"
            content = chunk.get("content", "").strip()
            if content:
                formatted_messages.append(f"[{i}] {role}: {content}")

        if not formatted_messages:
            return None

        input_text = "\n".join(formatted_messages)

        # Call Memory Worker LLM
        try:
            print(f"  [Memory Worker] Analyzing {len(chunks)} messages...")
            response = await self.llm.generate(
                messages=[{"role": "user", "content": input_text}],
                system_prompt=self.MEMORY_WORKER_SYSTEM_PROMPT,
                max_tokens=settings.NIGHT_LLM_MAX_TOKENS,
                temperature=settings.NIGHT_LLM_TEMPERATURE,
            )

            raw_output = response.get("thoughts", "").strip()
            
            # Try to parse JSON from the response
            # The model might wrap JSON in markdown code blocks
            json_str = self._extract_json(raw_output)
            if not json_str:
                print(f"  [WARNING] No valid JSON in response. Falling back to raw summary.")
                return {
                    "episode_summary": raw_output[:500],
                    "facts": [],
                    "keywords": [],
                    "topics": []
                }

            data = json.loads(json_str)
            return data

        except Exception as e:
            print(f"  [ERROR] Memory Worker failed: {e}")
            return None

    def _extract_json(self, text: str) -> Optional[str]:
        """Extract JSON object from text, handling markdown code blocks."""
        # Try to find JSON in ```json ... ``` block first
        import re
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if json_match:
            candidate = json_match.group(1).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

        # Try to find {...} directly
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            candidate = brace_match.group(0).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

        return None

    # ── Day Summary ────────────────────────────────────────────────────

    async def _create_day_summary(self, episode_summaries: List[str]) -> str:
        """Combine multiple episode summaries into one coherent day summary."""
        if not episode_summaries:
            return "[Нет записей за день]"

        if len(episode_summaries) == 1:
            return episode_summaries[0]

        combined = "\n".join(f"- {s}" for s in episode_summaries)
        input_text = f"Episode summaries:\n{combined}"

        try:
            response = await self.llm.generate(
                messages=[{"role": "user", "content": input_text}],
                system_prompt=self.DAY_SUMMARY_SYSTEM_PROMPT,
                max_tokens=500,
                temperature=0.3,
            )
            summary = response.get("thoughts", "").strip()
            return summary or episode_summaries[-1]  # fallback to last episode
        except Exception as e:
            print(f"  [ERROR] Day summary failed: {e}")
            return episode_summaries[-1]  # fallback


async def main():
    """Entry point for the offline processor."""
    pipeline = NightPipeline()
    await pipeline.run()


if __name__ == "__main__":
    asyncio.run(main())
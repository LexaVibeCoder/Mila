# core/pipeline.py
"""
Single-stage reasoning pipeline:
Model calls `think(thoughts, actions)` — system executes actions.

Flow:
1. LLM is called with tool_choice="required" and the `think` function.
2. Model returns thoughts + list of actions.
3. System executes actions:
   - send_message → immediately ready to send (no re-call needed)
   - search_memory → executes search, adds results to context, re-calls model
4. Loop continues until no more search_memory actions or max iterations.

Each turn produces a detailed JSON log saved to LOG_DIR.
"""

import json
import os
import time
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime

from config import settings
from core.llm_client import LLMClient
from core.tools import ToolExecutor, get_tool_definitions
from core.persona import build_main_llm_system_prompt
from core.embedding_client import EmbeddingClient
from core.memory import VectorMemory


# Try to import Observatory modules for dynamic config
try:
    from Mila_observatory.llm_manager import llm_manager
    from Mila_observatory.runtime import runtime_config
    HAS_OBSERVATORY = True
except ImportError:
    HAS_OBSERVATORY = False

MAX_THINK_ITERATIONS = 5


class ThinkPipeline:
    """Single-stage reasoning pipeline using `think` function."""

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

        self.main_llm = LLMClient()

    async def process_message(
        self,
        user_message: str,
        user_visible_history: List[Dict[str, str]],
        agent_internal_history: List[Dict[str, str]],
        user_profile: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Main entry point. Process user message through single think stage.

        Returns:
        {
            "success": bool,
            "actions": List[Dict] — executed tool calls,
            "thoughts": str — LLM's final thinking monologue,
            "error": Optional[str],
            "timing": Dict with stage times
        }
        """
        total_start = time.time()

        try:
            # Build system prompt and tool definitions
            main_system_prompt = build_main_llm_system_prompt(
                self.user_id, chat_id=self.chat_id, my_user_id=self.my_user_id
            )
            tool_definitions = get_tool_definitions()

            # Build messages from history
            # NOTE: agent_internal_history already contains the current user message
            # (it was pushed to context_buffer before _build_histories was called),
            # and is already token-limited to CONTEXT_MAX_TOKENS (30K).
            # We do NOT append user_message again to avoid duplication,
            # and we do NOT truncate — the token limit is already applied.
            messages = list(agent_internal_history)

            # Run the think loop
            all_actions = []
            final_thoughts = ""
            iteration = 0
            raw_llm_responses = []  # Store full raw LLM responses from each iteration

            while iteration < MAX_THINK_ITERATIONS:
                iteration += 1
                print(f"\n── Iteration {iteration}/{MAX_THINK_ITERATIONS} ──")

                # Call LLM with think function (tool_choice="required")
                response = await self.main_llm.generate(
                    messages=messages,
                    system_prompt=main_system_prompt,
                    tools=tool_definitions,
                    tool_choice="required",
                    max_tokens=2048,
                    temperature=self.main_llm.default_temperature,
                )

                thoughts = response.get("thoughts", "")
                tool_calls = response.get("tool_calls", None)

                # Brief console log — full details are in the JSON log file
                thoughts_preview = (thoughts or "")[:120].replace("\n", " ")
                print(f"  Thoughts: {thoughts_preview}...")
                if tool_calls:
                    for tc in tool_calls:
                        print(f"  → {tc.function.name}()")

                final_thoughts = thoughts

                # Store raw response for logging
                raw_llm_responses.append({
                    "iteration": iteration,
                    "thoughts": thoughts,
                    "tool_calls": [
                        {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                        for tc in (tool_calls or [])
                    ],
                    "finish_reason": response.get("finish_reason")
                })

                # Validate that think() was called
                if not tool_calls:
                    if iteration == 1:
                        print("  [Retry: no think() call]")
                        messages.append({
                            "role": "system",
                            "content": "ОШИБКА: Ты не вызвала функцию think(). Ты ОБЯЗАНА вызвать think() в каждом ответе. "
                                       "Пожалуйста, перегенерируй ответ и обязательно вызови think() с мыслями и действиями."
                        })
                        continue
                    else:
                        break

                first_tool = tool_calls[0]
                if not hasattr(first_tool, 'function') or first_tool.function.name != 'think':
                    if iteration == 1:
                        print(f"  [Retry: called {first_tool.function.name} instead of think]")
                        messages.append({
                            "role": "system",
                            "content": f"ОШИБКА: Ты вызвала функцию '{first_tool.function.name}' вместо 'think'. "
                                       "Ты ОБЯЗАНА использовать функцию think() в каждом ответе. "
                                       "Пожалуйста, перегенерируй ответ и обязательно вызови think()."
                        })
                        continue
                    else:
                        break

                # Parse actions from the think function call
                actions_from_llm = await self._parse_think_call(tool_calls[0])
                if not actions_from_llm:
                    print("  [Parse error]")
                    break

                action_types = [a.get("action_type", "?") for a in actions_from_llm]
                print(f"  Actions: {', '.join(action_types)}")

                # Execute actions
                has_search = False
                for action in actions_from_llm:
                    executed = await self._execute_single_action(action)
                    all_actions.append(executed)

                    if executed.get("type") == "search_memory" and executed.get("status") == "searched":
                        has_search = True
                        context_addon = executed.get("context_addon", "[Ничего не найдено]")
                        messages.append({"role": "system", "content": f"Результаты поиска в памяти:\n{context_addon}"})
                        print("  [Search results → re-calling]")

                if not has_search:
                    print("  [Done]")
                    break

            # End of think loop
            s_time = time.time() - total_start

            result = {
                "success": True,
                "actions": all_actions,
                "thoughts": final_thoughts,
                "system_prompt": main_system_prompt,
                "llm_messages": messages,
                "error": None,
                "raw_llm_responses": raw_llm_responses,
                "timing": {
                    "total": f"{s_time:.2f}s",
                    "iterations": iteration
                }
            }

            # Save detailed JSON log
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
                "error": str(e),
                "timing": {"total": f"{total_time:.2f}s"}
            }
            await self._save_log({**error_result, "traceback": tb}, user_message)
            return error_result

    # ------------------------------------------------------------------
    # Parse think() function call
    # ------------------------------------------------------------------

    async def _parse_think_call(self, tool_call) -> List[Dict]:
        """
        Parse the `think` function call arguments.
        Returns list of action dicts.
        """
        try:
            arguments = json.loads(tool_call.function.arguments)
            actions = arguments.get("actions", [])
            if not isinstance(actions, list):
                print(f"[ThinkPipeline] 'actions' is not a list: {type(actions)}")
                return []
            return actions
        except (json.JSONDecodeError, AttributeError, KeyError) as e:
            print(f"[ThinkPipeline] Failed to parse think() arguments: {e}")
            print(f"[ThinkPipeline] Raw arguments: {tool_call.function.arguments}")
            return []

    # ------------------------------------------------------------------
    # Execute single action
    # ------------------------------------------------------------------

    async def _execute_single_action(self, action: Dict) -> Dict:
        """Execute a single action from think()."""
        executor = ToolExecutor(
            text_memory=self.text_memory,
            embedding_client=self.emb_client,
            current_user_id=self.user_id,
            chat_id=self.chat_id
        )
        return await executor.execute_action(action)

    # ------------------------------------------------------------------
    # Detailed logging to file
    # ------------------------------------------------------------------

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
            "actions": result.get("actions", []),
            "raw_llm_responses": result.get("raw_llm_responses", []),
            "success": result.get("success", False),
            "error": result.get("error"),
            "timing": result.get("timing", {}),
            "traceback": result.get("traceback", None)
        }

        filename = f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        filepath = os.path.join(log_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(log_entry, f, ensure_ascii=False, indent=2, default=str)
            print(f"[Log] Saved to {filepath}")
        except Exception as e:
            print(f"[Log Error] Failed to save log: {e}")
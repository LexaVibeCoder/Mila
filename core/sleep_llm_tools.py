"""
core/sleep_llm_tools.py — LLM tool definitions for sleep processes.

During sleep, three LLM processes run, each with its own tool set.
LLMs ONLY interact with the system through these tools — no JSON responses.

Tools are divided into three categories matching the three sleep processes:

1. Diary Analysis: add/update/delete mila_memory_chunks
2. Dialog Summarization: create summaries in context_buffer
3. User Fact Extraction: add/update facts in user memories
"""

import json
from typing import List, Dict, Any, Optional


# ═══════════════════════════════════════════════════════════════
# 1. TOOLS FOR DIARY ANALYSIS (process 1)
# ═══════════════════════════════════════════════════════════════

def get_diary_analysis_tools() -> List[Dict]:
    """Tool definitions for diary analysis process.
    
    LLM reads the diary, gets current eternal memory chunks,
    and can add/update/delete chunks.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "add_memory_chunk",
                "description": "Add a new chunk to Mila's eternal memory. "
                               "Use this for genuinely important life events that should be remembered forever.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short title for this memory chunk (e.g., 'Realization about solitude')"
                        },
                        "content_md": {
                            "type": "string",
                            "description": "Full content in Markdown format. Describe the event, realization, or experience."
                        },
                        "importance": {
                            "type": "number",
                            "description": "Importance level from 0.0 (low) to 1.0 (critical life event)",
                            "minimum": 0.0,
                            "maximum": 1.0
                        }
                    },
                    "required": ["title", "content_md", "importance"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "update_memory_chunk",
                "description": "Update an existing memory chunk. Use this when a past memory needs to be "
                               "revised due to new understanding or personality development. "
                               "This updates the chunk in-place with a new version.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chunk_id": {
                            "type": "string",
                            "description": "The chunk_id of the memory to update"
                        },
                        "title": {
                            "type": "string",
                            "description": "New title (optional)"
                        },
                        "content_md": {
                            "type": "string",
                            "description": "New content in Markdown format"
                        },
                        "importance": {
                            "type": "number",
                            "description": "New importance level (optional)",
                            "minimum": 0.0,
                            "maximum": 1.0
                        }
                    },
                    "required": ["chunk_id", "content_md"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_memory_chunk",
                "description": "Deactivate (soft-delete) an outdated or irrelevant memory chunk. "
                               "Use this when a memory is no longer relevant to Mila's current self.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chunk_id": {
                            "type": "string",
                            "description": "The chunk_id of the memory to deactivate"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this chunk is being deactivated"
                        }
                    },
                    "required": ["chunk_id", "reason"]
                }
            }
        }
    ]


def get_diary_analysis_tool_names() -> List[str]:
    """Return the list of tool names for diary analysis."""
    return ["add_memory_chunk", "update_memory_chunk", "delete_memory_chunk"]


# ═══════════════════════════════════════════════════════════════
# 2. TOOLS FOR DIALOG SUMMARIZATION (process 2)
# ═══════════════════════════════════════════════════════════════

def get_dialog_summarization_tools() -> List[Dict]:
    """Tool definitions for dialog summarization process.
    
    LLM reads dialog messages, creates a summary, and marks them for cleanup.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "save_dialog_summary",
                "description": "Save a summary of a dialog to the context buffer. "
                               "After this, the old messages will be removed. "
                               "Call this only when there are enough messages to justify summarization.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "The chat_id of the dialog being summarized"
                        },
                        "summary_text": {
                            "type": "string",
                            "description": "A concise summary of the dialog (2-4 sentences). "
                                           "Include key topics, decisions, and emotional context."
                        },
                        "message_count": {
                            "type": "integer",
                            "description": "How many messages were summarized"
                        }
                    },
                    "required": ["chat_id", "summary_text", "message_count"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "skip_dialog",
                "description": "Skip this dialog — not enough messages to summarize or dialog is not ready.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "The chat_id of the dialog to skip"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Reason for skipping"
                        }
                    },
                    "required": ["chat_id", "reason"]
                }
            }
        }
    ]


def get_dialog_summarization_tool_names() -> List[str]:
    """Return the list of tool names for dialog summarization."""
    return ["save_dialog_summary", "skip_dialog"]


# ═══════════════════════════════════════════════════════════════
# 3. TOOLS FOR USER FACT EXTRACTION (process 3)
# ═══════════════════════════════════════════════════════════════

def get_user_fact_extraction_tools() -> List[Dict]:
    """Tool definitions for user fact extraction process.
    
    LLM reads dialog messages and extracts long-term facts about users.
    Supports group chats — facts must be attributed to the correct user.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "add_user_fact",
                "description": "Add a long-term fact about a user to their memory. "
                               "Only store facts that are genuinely important for long-term relationship. "
                               "For group chats, ensure the user_id matches the author of the message.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "The user_id this fact belongs to"
                        },
                        "content": {
                            "type": "string",
                            "description": "The fact to remember. Be specific and factual."
                        },
                        "importance": {
                            "type": "number",
                            "description": "Importance level from 0.0 (trivial) to 1.0 (critical)",
                            "minimum": 0.0,
                            "maximum": 1.0
                        },
                        "source_chat_id": {
                            "type": "string",
                            "description": "The chat_id where this fact was learned"
                        }
                    },
                    "required": ["user_id", "content", "importance", "source_chat_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "skip_user_fact_extraction",
                "description": "Skip fact extraction for this dialog — no meaningful facts found.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "The chat_id being analyzed"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why no facts were extracted"
                        }
                    },
                    "required": ["chat_id", "reason"]
                }
            }
        }
    ]


def get_user_fact_extraction_tool_names() -> List[str]:
    """Return the list of tool names for user fact extraction."""
    return ["add_user_fact", "skip_user_fact_extraction"]


# ═══════════════════════════════════════════════════════════════
# 4. VERIFICATION TOOLS (used by verifier LLM)
# ═══════════════════════════════════════════════════════════════

def get_verification_tools() -> List[Dict]:
    """Tool definitions for the verification stage.
    
    The verifier LLM checks the output of the first LLM and either
    approves or rejects it.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "approve_changes",
                "description": "Approve the changes made by the generation LLM. "
                               "Call this only if the changes are correct, relevant, and well-formed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "process_type": {
                            "type": "string",
                            "enum": ["diary_analysis", "dialog_summarization", "user_fact_extraction"],
                            "description": "Which process's changes are being approved"
                        },
                        "notes": {
                            "type": "string",
                            "description": "Any notes about the approval"
                        }
                    },
                    "required": ["process_type", "notes"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "reject_changes",
                "description": "Reject the changes and request a new iteration. "
                               "Call this if the changes contain errors, are irrelevant, or poorly formed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "process_type": {
                            "type": "string",
                            "enum": ["diary_analysis", "dialog_summarization", "user_fact_extraction"],
                            "description": "Which process's changes are being rejected"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Detailed explanation of why the changes are rejected"
                        },
                        "correction_hint": {
                            "type": "string",
                            "description": "Optional hint for what to do differently"
                        }
                    },
                    "required": ["process_type", "reason"]
                }
            }
        }
    ]


def get_verification_tool_names() -> List[str]:
    """Return the list of tool names for verification."""
    return ["approve_changes", "reject_changes"]
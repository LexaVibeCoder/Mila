"""
core/sleep/tools.py — LLM tool definitions for sleep processes.
"""

import json
from typing import List, Dict, Any, Optional


def get_diary_analysis_tools() -> List[Dict]:
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
    return ["add_memory_chunk", "update_memory_chunk", "delete_memory_chunk"]


def get_dialog_summarization_tools() -> List[Dict]:
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
    return ["save_dialog_summary", "skip_dialog"]


def get_user_fact_extraction_tools() -> List[Dict]:
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
    return ["add_user_fact", "skip_user_fact_extraction"]


def get_verification_tools() -> List[Dict]:
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
    return ["approve_changes", "reject_changes"]

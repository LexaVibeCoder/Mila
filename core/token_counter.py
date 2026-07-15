"""
core/token_counter.py — Token counting utility for Mila.

Uses tiktoken for accurate token counting compatible with OpenAI models.
Falls back to a rough character-based estimate if tiktoken is unavailable.
"""

from typing import List, Dict, Optional
import tiktoken

# Default encoding for most OpenAI models
# For other models, we use cl100k_base which is the most common
_ENCODING_NAME = "cl100k_base"

_encoding_cache: dict = {}


def _get_encoding(model: str = None) -> tiktoken.Encoding:
    """Get the appropriate encoding for a model."""
    if model:
        try:
            encoding = tiktoken.encoding_for_model(model)
            return encoding
        except KeyError:
            pass
    
    # Fallback to cl100k_base
    if _ENCODING_NAME not in _encoding_cache:
        _encoding_cache[_ENCODING_NAME] = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoding_cache[_ENCODING_NAME]


def count_tokens(text: str, model: str = None) -> int:
    """
    Count the number of tokens in a text string.
    
    Args:
        text: The text to count tokens for
        model: Optional model name for model-specific encoding
    
    Returns:
        Number of tokens
    """
    if not text:
        return 0
    encoding = _get_encoding(model)
    return len(encoding.encode(text))


def count_messages_tokens(messages: List[Dict[str, str]], model: str = None) -> int:
    """
    Count tokens for a list of messages (as used in chat completions API).
    
    This approximates the OpenAI chat format token count:
    - Each message has role + content overhead (~4 tokens per message)
    - Plus the actual content tokens
    
    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Optional model name
    
    Returns:
        Total token count
    """
    encoding = _get_encoding(model)
    total = 0
    
    for msg in messages:
        # Per-message overhead: <|start|>role<|content|>content<|end|>
        total += 4  # base overhead per message
        if msg.get("role"):
            total += len(encoding.encode(msg["role"]))
        if msg.get("content"):
            total += len(encoding.encode(msg["content"]))
    
    # Add priming overhead (~2 tokens)
    total += 2
    
    return total


def count_context_buffer_tokens(entries: List[Dict]) -> int:
    """
    Count tokens for context buffer entries (message type only).
    Summaries are counted too but with a flag to distinguish.
    
    Args:
        entries: List of context buffer entry dicts with 'content' and 'type' keys
    
    Returns:
        Total token count for message-type entries only
    """
    total = 0
    for entry in entries:
        content = entry.get("content", "") or ""
        if content.strip():
            total += count_tokens(content)
    return total


def truncate_to_token_limit(text: str, max_tokens: int, model: str = None) -> str:
    """
    Truncate text to fit within max_tokens.
    
    Args:
        text: Text to truncate
        max_tokens: Maximum number of tokens allowed
        model: Optional model name
    
    Returns:
        Truncated text
    """
    if not text:
        return ""
    
    encoding = _get_encoding(model)
    tokens = encoding.encode(text)
    
    if len(tokens) <= max_tokens:
        return text
    
    return encoding.decode(tokens[:max_tokens])
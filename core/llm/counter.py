from typing import List, Dict, Optional
import tiktoken

_ENCODING_NAME = "cl100k_base"
_encoding_cache: dict = {}


def _get_encoding(model: str = None) -> tiktoken.Encoding:
    if model:
        try:
            encoding = tiktoken.encoding_for_model(model)
            return encoding
        except KeyError:
            pass
    if _ENCODING_NAME not in _encoding_cache:
        _encoding_cache[_ENCODING_NAME] = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoding_cache[_ENCODING_NAME]


def count_tokens(text: str, model: str = None) -> int:
    if not text:
        return 0
    encoding = _get_encoding(model)
    return len(encoding.encode(text))


def count_messages_tokens(messages: List[Dict[str, str]], model: str = None) -> int:
    encoding = _get_encoding(model)
    total = 0

    for msg in messages:
        total += 4
        if msg.get("role"):
            total += len(encoding.encode(msg["role"]))
        if msg.get("content"):
            total += len(encoding.encode(msg["content"]))

    total += 2
    return total


def count_context_buffer_tokens(entries: List[Dict]) -> int:
    total = 0
    for entry in entries:
        content = entry.get("content", "") or ""
        if content.strip():
            total += count_tokens(content)
    return total


def truncate_to_token_limit(text: str, max_tokens: int, model: str = None) -> str:
    if not text:
        return ""

    encoding = _get_encoding(model)
    tokens = encoding.encode(text)

    if len(tokens) <= max_tokens:
        return text

    return encoding.decode(tokens[:max_tokens])

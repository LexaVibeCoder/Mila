"""Shared TDLib (pytdbot) helpers.

Isolates all pytdbot-specific details so the rest of the codebase
(pipeline, db, sleep) works with plain Python types.
Python 3.11 compatible.
"""

from __future__ import annotations

from dataclasses import dataclass

from pytdbot import types


@dataclass
class NormalizedMessage:
    chat_id: str
    user_id: str
    text: str
    message_id: int = 0
    sender_name: str = "Unknown"
    chat_name: str = "Личный чат"


def is_text_message(message: types.Message) -> bool:
    """True for plain text messages (replaces pyrogram filters.text)."""
    return isinstance(message.content, types.MessageText) and bool(
        (message.text or "").strip()
    )


async def is_private_chat(client, chat_id: int) -> bool:
    """True if the chat is a 1-1 user chat (replaces filters.private)."""
    chat = await client.getChat(chat_id=chat_id)
    if isinstance(chat, types.Error):
        return False
    return isinstance(chat.type, types.ChatTypePrivate)


async def resolve_sender_name(client, message: types.Message) -> str:
    from_id = message.from_id
    if not from_id:
        return "Unknown"
    user = await client.getUser(user_id=from_id)
    if isinstance(user, types.Error):
        return "Unknown"
    name = (user.first_name or "").strip()
    if user.last_name:
        name = (name + " " + user.last_name).strip()
    return name or "Unknown"


async def resolve_chat_name(client, message: types.Message) -> str:
    chat = await client.getChat(chat_id=message.chat_id)
    if isinstance(chat, types.Error):
        return "Личный чат"
    if isinstance(chat.type, types.ChatTypePrivate):
        # For private chats the sender name is more informative.
        sender_name = await resolve_sender_name(client, message)
        return sender_name if sender_name != "Unknown" else (chat.title or "Личный чат")
    return chat.title or "Личный чат"


async def normalize_message(client, message: types.Message) -> NormalizedMessage:
    text = (message.text or "").strip()
    sender_name = await resolve_sender_name(client, message)
    chat_name = await resolve_chat_name(client, message)
    return NormalizedMessage(
        chat_id=str(message.chat_id),
        user_id=str(message.from_id or message.chat_id),
        text=text,
        message_id=message.id,
        sender_name=sender_name,
        chat_name=chat_name,
    )


async def send_text(client, chat_id: int, text: str) -> None:
    """Send a text message, raising on TDLib error."""
    res = await client.sendTextMessage(chat_id=chat_id, text=text)
    if isinstance(res, types.Error):
        raise RuntimeError(f"TDLib sendTextMessage failed: {res.code} {res.message}")


async def send_typing(client, chat_id: int) -> None:
    res = await client.sendChatAction(
        chat_id=chat_id,
        action=types.ChatActionTyping(),
    )
    if isinstance(res, types.Error):
        # Typing is best-effort; ignore rate limits / closed chats.
        pass


# ------------------------------------------------------------------
# Peer typing (updateChatAction) helpers.
#
# TDLib emits updateChatAction(chat_id, sender_id, action) whenever the
# peer starts/stops typing, recording, uploading, etc.
# We treat any non-cancel action as "user is typing" and ChatActionCancel
# (or expiry) as "stopped".
# ------------------------------------------------------------------

def _td_type_name(obj) -> str:
    """Best-effort TDLib type name without importing internals."""
    t = type(obj).__name__
    get_type = getattr(obj, "getType", None)
    if callable(get_type):
        try:
            return str(get_type())
        except Exception:
            return t
    # pytdbot objects often carry "@type".
    at = getattr(obj, "_type", None) or getattr(obj, "type", None)
    if isinstance(at, str):
        return at
    return t


def is_cancel_action(action) -> bool:
    """True if the chat action means 'peer stopped' (ChatActionCancel)."""
    if action is None:
        return True
    name = _td_type_name(action).lower()
    return "cancel" in name


def is_typing_like_action(action) -> bool:
    """
    True for any live peer activity: typing, recording, uploading, playing, etc.
    False for cancel / None.
    """
    if action is None:
        return False
    return not is_cancel_action(action)


def extract_sender_user_id(sender_id) -> int | None:
    """
    Extract a numeric user id from a MessageSender object.

    Returns None for channel/chat senders (MessageSenderChat) or unknown shapes.
    Duck-typed so it keeps working across pytdbot versions.
    """
    if sender_id is None:
        return None
    # MessageSenderUser(user_id=...)
    uid = getattr(sender_id, "user_id", None)
    if isinstance(uid, int) and uid:
        return uid
    # Fallbacks for dict-like / differently named fields.
    if isinstance(sender_id, dict):
        uid = sender_id.get("user_id")
        if isinstance(uid, int) and uid:
            return uid
    name = _td_type_name(sender_id).lower()
    if "user" in name and isinstance(uid, int):
        return uid
    return None


def parse_chat_action_update(update) -> tuple[int | None, int | None, object]:
    """
    Parse an UpdateChatAction into (chat_id, sender_user_id, action).

    Never raises — returns (None, None, None) on unexpected shapes.
    """
    try:
        chat_id = getattr(update, "chat_id", None)
        if not isinstance(chat_id, int):
            return None, None, None
        sender_id = getattr(update, "sender_id", None)
        action = getattr(update, "action", None)
        return chat_id, extract_sender_user_id(sender_id), action
    except Exception:
        return None, None, None

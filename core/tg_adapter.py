"""Shared TDLib (pytdbot) helpers.

Isolates all pytdbot-specific details so the rest of the codebase
(pipeline, db, sleep) works with plain Python types.
Python 3.11 compatible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from pytdbot import types

from core.events import Notification


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
    """True if the chat is a 1-1 user chat (replaces filters.private).

    Transient TDLib errors are retried once so one network blip doesn't
    silently drop a message. Persistent errors still return False (fail
    closed — don't treat unknown chats as private).
    """
    import asyncio
    last_err = None
    for attempt in range(2):
        try:
            chat = await client.getChat(chat_id=chat_id)
        except Exception as e:
            last_err = e
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            print(f"[TG] getChat failed for {chat_id}: {e}")
            return False
        if isinstance(chat, types.Error):
            # Rate-limited / transient: retry once.
            msg = f"{chat.code} {chat.message}".lower()
            transient = chat.code == 429 or "timeout" in msg or "temporary" in msg or "flood" in msg
            if transient and attempt == 0:
                await asyncio.sleep(1.5)
                continue
            if attempt == 0 and chat.code not in (404, 400):
                # Unknown error shape — one retry before failing closed.
                await asyncio.sleep(1.0)
                continue
            return False
        return isinstance(chat.type, types.ChatTypePrivate)
    print(f"[TG] getChat failed for {chat_id}: {last_err}")
    return False


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
    """Send a text message with one retry on 429/flood, raising on failure."""
    import asyncio
    import random
    last_error: Exception | None = None
    for attempt in range(3):
        res = await client.sendTextMessage(chat_id=chat_id, text=text)
        if not isinstance(res, types.Error):
            return
        msg = f"{res.code} {res.message}"
        low = msg.lower()
        last_error = RuntimeError(f"TDLib sendTextMessage failed: {msg}")
        retryable = res.code == 429 or "flood" in low or "retry" in low or "timeout" in low or "temporary" in low or res.code >= 500
        if retryable and attempt < 2:
            # Honor flood wait when TDLib reports it (e.g. "retry after 3").
            wait = 1.5 * (2 ** attempt) + random.uniform(0, 0.5)
            try:
                import re
                m = re.search(r"retry after (\d+)", low)
                if m:
                    wait = min(max(int(m.group(1)), 1), 30) + random.uniform(0, 0.5)
            except Exception:
                pass
            print(f"[TG] send retry in {wait:.1f}s ({msg})")
            await asyncio.sleep(wait)
            continue
        raise last_error
    if last_error:
        raise last_error


async def send_typing(client, chat_id: int) -> None:
    res = await client.sendChatAction(
        chat_id=chat_id,
        action=types.ChatActionTyping(),
    )
    if isinstance(res, types.Error):
        # Typing is best-effort; ignore rate limits / closed chats.
        pass


# ------------------------------------------------------------------
# Multimodal notifications (v1: text / photo / voice, rest as stub).
#
# Every incoming private message becomes a Notification, never dropped.
# Media bytes are downloaded via downloadFile(synchronous=True) and passed
# to the LLM natively; text pipeline keeps only placeholder_text().
# ------------------------------------------------------------------

def _content_attr(content, *names):
    """Duck-typed getattr: works across pytdbot versions."""
    for n in names:
        v = getattr(content, n, None)
        if v is not None:
            return v
    if isinstance(content, dict):
        for n in names:
            if n in content:
                return content[n]
    return None


def _formatted_text_to_str(ft) -> str:
    if ft is None:
        return ""
    if isinstance(ft, str):
        return ft.strip()
    t = getattr(ft, "text", None)
    if isinstance(t, str):
        return t.strip()
    if isinstance(ft, dict):
        return str(ft.get("text", "") or "").strip()
    return ""


def classify_message_content(message) -> tuple[str, str, Optional[int], str]:
    """Return (kind, caption, file_id, extra_hint). Never raises."""
    try:
        content = getattr(message, "content", None)
        if content is None:
            return "unsupported", "", None, ""
        cname = type(content).__name__
        if isinstance(content, dict):
            cname = str(content.get("@type", cname))
        low = cname.lower()

        # Plain text
        if "messagetext" in low:
            txt = getattr(message, "text", "") or _formatted_text_to_str(
                _content_attr(content, "text"))
            return "text", (txt or "").strip(), None, ""

        # Photo: content.photo.sizes[-1].photo (File) + caption
        if "messagephoto" in low:
            photo = _content_attr(content, "photo")
            caption = _formatted_text_to_str(_content_attr(content, "caption"))
            fid = _pick_biggest_photo_file_id(photo)
            return "photo", caption, fid, ""

        # Voice note: content.voice_note.voice (File)
        if "messagevoicenote" in low:
            vn = _content_attr(content, "voice_note", "voiceNote", "voice")
            caption = _formatted_text_to_str(_content_attr(content, "caption"))
            fid = _extract_file_id(vn, ("voice", "file", "audio"))
            return "voice", caption, fid, ""

        # Video / video note — placeholder in v1
        if "messagevideonote" in low:
            return "video_note", "", None, ""
        if "messagevideo" in low:
            caption = _formatted_text_to_str(_content_attr(content, "caption"))
            return "video", caption, None, ""
        if "messagesticker" in low:
            st = _content_attr(content, "sticker")
            emoji = ""
            set_name = ""
            if st is not None:
                emoji = getattr(st, "emoji", "") or (st.get("emoji", "") if isinstance(st, dict) else "")
                set_name = getattr(st, "set_name", "") or (st.get("set_name", "") if isinstance(st, dict) else "")
            hint = " ".join(x for x in [emoji, f"({set_name})" if set_name else ""] if x).strip()
            return "sticker", "", None, hint
        if "messageanimation" in low:
            caption = _formatted_text_to_str(_content_attr(content, "caption"))
            return "animation", caption, None, ""
        if "messageaudio" in low:
            caption = _formatted_text_to_str(_content_attr(content, "caption"))
            return "audio", caption, None, ""
        if "messagedocument" in low:
            caption = _formatted_text_to_str(_content_attr(content, "caption"))
            return "document", caption, None, ""
        return "unsupported", "", None, ""
    except Exception:
        return "unsupported", "", None, ""


def _pick_biggest_photo_file_id(photo) -> Optional[int]:
    try:
        if photo is None:
            return None
        sizes = getattr(photo, "sizes", None)
        if isinstance(photo, dict):
            sizes = photo.get("sizes", [])
        if not sizes:
            return None
        biggest = max(sizes, key=lambda s: (
            (getattr(s, "width", 0) or (s.get("width", 0) if isinstance(s, dict) else 0)) *
            (getattr(s, "height", 0) or (s.get("height", 0) if isinstance(s, dict) else 0))
        ))
        inner = getattr(biggest, "photo", None)
        if isinstance(biggest, dict):
            inner = biggest.get("photo", {})
        return _extract_file_id(inner, ("photo", "file"))
    except Exception:
        return None


def _extract_file_id(obj, keys=("file",)) -> Optional[int]:
    try:
        if obj is None:
            return None
        if isinstance(obj, int):
            return obj
        for k in keys:
            v = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
            if isinstance(v, int) and v:
                return v
            if v is not None and not isinstance(v, (str, bytes)):
                fid = getattr(v, "id", None) if not isinstance(v, dict) else v.get("id")
                if isinstance(fid, int) and fid:
                    return fid
        direct = getattr(obj, "id", None) if not isinstance(obj, dict) else obj.get("id")
        if isinstance(direct, int) and direct:
            return direct
        return None
    except Exception:
        return None


async def normalize_event(client, message) -> Notification:
    """Build a Notification for ANY private message. Never drops, never raises."""
    try:
        sender_name = await resolve_sender_name(client, message)
    except Exception:
        sender_name = "Unknown"
    try:
        chat_name = await resolve_chat_name(client, message)
    except Exception:
        chat_name = "Личный чат"
    kind, caption, file_id, hint = classify_message_content(message)
    from_id = getattr(message, "from_id", None) or getattr(message, "sender_id", None)
    if isinstance(from_id, dict):
        from_id = from_id.get("user_id", getattr(message, "chat_id", 0))
    try:
        user_id = str(int(from_id)) if from_id else str(message.chat_id)
    except (TypeError, ValueError):
        user_id = str(getattr(message, "chat_id", "?"))
    # Text fallback for caption-less text messages
    if kind == "text" and not caption:
        try:
            caption = (getattr(message, "text", "") or "").strip()
        except Exception:
            caption = ""
    return Notification(
        kind=kind,
        chat_id=str(getattr(message, "chat_id", "?")),
        user_id=user_id,
        text=caption,
        message_id=int(getattr(message, "id", 0) or 0),
        sender_name=sender_name,
        chat_name=chat_name,
        file_id=file_id,
        extra_hint=hint,
    )


async def download_notification_media(client, notif: Notification,
                                      max_mb: int = 20) -> Optional[str]:
    """Download media for photo/voice via TDLib. Returns local path or None."""
    if notif.kind not in ("photo", "voice") or not notif.file_id:
        return None
    try:
        from config import settings
        media_dir = getattr(settings, "MEDIA_DIR", "data/media")
        os.makedirs(media_dir, exist_ok=True)
        res = await client.downloadFile(
            file_id=int(notif.file_id), priority=16, synchronous=True)
        if isinstance(res, types.Error):
            print(f"[Media] downloadFile failed: {res.code} {res.message}")
            return None
        local = getattr(getattr(res, "local", None), "path", None)
        if isinstance(res, dict):
            local = ((res.get("local") or {}).get("path"))
        if not local or not os.path.isfile(local):
            return None
        if os.path.getsize(local) > max_mb * 1024 * 1024:
            print(f"[Media] file too big, skipping: {local}")
            return None
        return local
    except Exception as e:
        print(f"[Media] download error: {e}")
        return None


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

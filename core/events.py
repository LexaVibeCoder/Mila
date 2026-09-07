"""Unified incoming event (notification) model.

Every Telegram update becomes a Notification — not a direct order to reply.
Mila sees the notification (state + last messages + this event) and decides
herself: enter dialog & answer, or stay silent in the current state.

First version covers: text, photo, voice (+ graceful placeholders for the rest).
Media bytes are passed to the LLM natively (vision / audio input) — no
separate captioner/STT in v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Kinds handled natively in v1
SUPPORTED_KINDS = {"text", "photo", "voice"}
# Kinds we store as placeholders for now (second stage)
PLACEHOLDER_KINDS = {
    "video", "video_note", "sticker",
    "animation", "audio", "document",
    "unsupported",
}


@dataclass
class Notification:
    kind: str  # text | photo | voice | video | sticker | ...
    chat_id: str
    user_id: str
    text: str = ""  # message text or caption (may be empty for media)
    message_id: int = 0
    sender_name: str = "Unknown"
    chat_name: str = "Личный чат"
    # TDLib file id for download (photo / voice). None for text.
    file_id: Optional[int] = None
    # Local downloaded path (filled after download, if any)
    local_path: Optional[str] = None
    # Extra human-readable hint, e.g. sticker emoji / set name
    extra_hint: str = ""

    def placeholder_text(self) -> str:
        """Text representation stored in chunks / context buffer.

        Keeps the text pipeline (history, summaries, memory search) working
        even for media: the native bytes go to the LLM separately, only for
        the current event.
        """
        caption = (self.text or "").strip()
        if self.kind == "text":
            return caption
        if self.kind == "photo":
            return f"[фото{(': ' + caption) if caption else ''}]"
        if self.kind == "voice":
            return f"[голосовое сообщение{(': ' + caption) if caption else ''}]"
        if self.kind == "video":
            return f"[видео{(': ' + caption) if caption else ''}]"
        if self.kind == "video_note":
            return "[видеосообщение-кружок]"
        if self.kind == "sticker":
            hint = f" {self.extra_hint}" if self.extra_hint else ""
            return f"[стикер{hint}]"
        if self.kind == "animation":
            return "[гифка]"
        if self.kind == "audio":
            return f"[аудио{(': ' + caption) if caption else ''}]"
        if self.kind == "document":
            return f"[файл{(': ' + caption) if caption else ''}]"
        return f"[сообщение типа {self.kind}{(': ' + caption) if caption else ''}]"

    def llm_event_line(self) -> str:
        """One-line description injected into the LLM context for this event."""
        who = self.sender_name or "Unknown"
        return (
            f"[уведомление] чат {self.chat_id} ({who}): "
            f"{self.placeholder_text() or '(без текста)'}"
        )

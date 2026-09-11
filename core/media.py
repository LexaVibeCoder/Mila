"""Media helpers for native vision/audio LLM input (v1: photo + voice).

History-aware: recent photos/audios are re-attached to their messages,
not only the last one. Audio is normalized to wav/mp3 (Muse Spark
officially understands only wav/mp3, Telegram sends ogg/opus).
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import time
import uuid
from typing import Optional


MAX_IMAGE_SIDE = 1568

# How many recent media items stay natively visible in history
# (total including the current event).
MAX_HISTORY_IMAGES = 3
MAX_HISTORY_AUDIOS = 2


def encode_image_for_llm(path: str) -> Optional[str]:
    """Resize + JPEG-encode a photo, return data_url. None on failure."""
    try:
        from PIL import Image
        img = Image.open(path)
        if img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"[Media] image encode failed: {e}")
        return None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def convert_audio_to_wav(src_path: str) -> Optional[str]:
    """Convert ogg/opus/m4a/flac to 16kHz mono wav via ffmpeg.

    Returns path to wav (cached next to src) or None if conversion failed.
    If src is already wav/mp3, returns src unchanged.
    If ffmpeg is missing, returns src unchanged (caller decides).
    """
    try:
        ext = os.path.splitext(src_path)[1].lower()
        if ext in (".wav", ".mp3"):
            return src_path
        if not _ffmpeg_available():
            print("[Media] ffmpeg not found, sending audio as-is (may degrade to transcription-only)")
            return src_path
        dst_path = os.path.splitext(src_path)[0] + ".wav"
        if os.path.isfile(dst_path) and os.path.getmtime(dst_path) >= os.path.getmtime(src_path):
            return dst_path
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", src_path,
            "-ac", "1", "-ar", "16000",
            dst_path,
        ]
        subprocess.run(cmd, timeout=60, check=True)
        if os.path.isfile(dst_path):
            return dst_path
        return None
    except Exception as e:
        print(f"[Media] ffmpeg convert failed: {e}")
        return None


def encode_audio_for_llm(path: str,
                         max_mb: int = 10) -> Optional[tuple[str, str]]:
    """Return (base64, format) for voice notes. None on failure/oversize.

    Normalizes to wav/mp3 only: Telegram ogg/opus is converted via ffmpeg.
    Muse Spark / OpenRouter officially support wav + mp3 for audio
    understanding; sending ogg degrades to speech-transcription without
    ambient sounds.
    """
    try:
        if not os.path.isfile(path):
            return None
        work_path = path
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "ogg"
        if ext not in ("wav", "mp3"):
            converted = convert_audio_to_wav(path)
            if converted and os.path.isfile(converted):
                work_path = converted
                ext = os.path.splitext(converted)[1].lower().lstrip(".") or "wav"
            else:
                print("[Media] audio convert failed, skipping native audio (keeping placeholder)")
                return None
        fmt = ext if ext in ("wav", "mp3") else "wav"
        if os.path.getsize(work_path) > max_mb * 1024 * 1024:
            print("[Media] audio too big for LLM, skipping native audio")
            return None
        with open(work_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return b64, fmt
    except Exception as e:
        print(f"[Media] audio encode failed: {e}")
        return None


def persist_media_file(src_path: str, chat_id: str, kind: str) -> Optional[str]:
    """Copy a downloaded TDLib file into MEDIA_DIR for long-lived history.

    TDLib paths may be GC'd; chunks.media_path must survive restarts so
    recent photos/audios can be re-attached to history on later turns.
    Returns persistent path or None.
    """
    try:
        from config import settings
        media_dir = getattr(settings, "MEDIA_DIR", "data/media")
        os.makedirs(media_dir, exist_ok=True)
        if not src_path or not os.path.isfile(src_path):
            return None
        ext = os.path.splitext(src_path)[1].lower() or (".jpg" if kind == "photo" else ".ogg")
        safe_chat = "".join(c if c.isalnum() else "_" for c in str(chat_id))
        name = f"{safe_chat}_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
        dst = os.path.join(media_dir, name)
        shutil.copy2(src_path, dst)
        # If audio, also keep a converted wav next to it for fast re-encode.
        if kind == "voice":
            try:
                convert_audio_to_wav(dst)
            except Exception:
                pass
        return dst
    except Exception as e:
        print(f"[Media] persist failed: {e}")
        return None


def build_multimodal_parts(text: str, image_data_urls: list[str] | None = None,
                           audio: tuple[str, str] | list[tuple[str, str]] | None = None) -> str | list:
    """Build OpenAI-compatible message content.

    Returns a plain string when no media, else a parts list:
    [{type: text}, {type: image_url}, {type: input_audio}].

    `audio` accepts a single (b64, fmt) tuple or a list of them.
    Media bytes are attached as-is; no extra instructions are added —
    the model decides itself what to do with a photo or audio.
    """
    parts: list = []
    audios: list[tuple[str, str]] = []
    if audio is not None:
        if isinstance(audio, list):
            audios = [a for a in audio if a]
        else:
            audios = [audio]
    if text and text.strip():
        parts.append({"type": "text", "text": text})
    for url in image_data_urls or []:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    for b64, fmt in audios:
        parts.append({"type": "input_audio",
                      "input_audio": {"data": b64, "format": fmt}})
    if not parts:
        return text or ""
    # Pure text stays a string (max compat with prompts/counters)
    if len(parts) == 1 and parts[0].get("type") == "text":
        return text
    return parts

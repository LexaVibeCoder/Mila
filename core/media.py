"""Media helpers for native vision/audio LLM input (v1: photo + voice)."""

from __future__ import annotations

import base64
import io
import os
from typing import Optional


MAX_IMAGE_SIDE = 1568


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


def encode_audio_for_llm(path: str,
                         max_mb: int = 10) -> Optional[tuple[str, str]]:
    """Return (base64, format) for voice notes. None on failure/oversize."""
    try:
        if not os.path.isfile(path):
            return None
        if os.path.getsize(path) > max_mb * 1024 * 1024:
            print("[Media] audio too big for LLM, skipping native audio")
            return None
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "ogg"
        # OpenAI-compatible audio input expects wav/mp3; most gateways
        # (incl. OpenRouter) forward ogg/opus too — try as is, fall back
        # to placeholder if the API rejects it.
        fmt = ext if ext in ("mp3", "wav", "ogg", "opus", "m4a", "flac") else "mp3"
        if fmt == "opus":
            fmt = "ogg"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return b64, fmt
    except Exception as e:
        print(f"[Media] audio encode failed: {e}")
        return None


def build_multimodal_parts(text: str, image_data_urls: list[str] | None = None,
                           audio: tuple[str, str] | None = None) -> str | list:
    """Build OpenAI-compatible message content.

    Returns a plain string when no media, else a parts list:
    [{type: text}, {type: image_url}, {type: input_audio}].
    """
    parts: list = []
    if text and text.strip():
        parts.append({"type": "text", "text": text})
    for url in image_data_urls or []:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if audio is not None:
        b64, fmt = audio
        parts.append({"type": "input_audio",
                      "input_audio": {"data": b64, "format": fmt}})
    if not parts:
        return text or ""
    # Pure text stays a string (max compat with prompts/counters)
    if len(parts) == 1 and parts[0].get("type") == "text":
        return text
    return parts

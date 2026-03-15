"""
domain/media.py – Sticker media processing pipeline for Stix Magic.

Handles:
  • Static image  → WEBP sticker (≤ 64 KB, max 512 px)
  • Video / GIF   → VP9 WEBM animated sticker (≤ 256 KB, max 3 s, 512 px)
  • Mask compositing (B&W mask → transparent cutout)

All synchronous processing functions are pure / near-pure: they receive
bytes / BytesIO and return BytesIO (or None on failure) so they can be
tested without a running bot.

Async wrappers (prefixed `async_`) offload blocking CPU/IO work to a
thread-pool executor so they never stall the asyncio event loop.
"""

import asyncio
import io
import logging
import os
import subprocess
import tempfile

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)


# ── File-type detection ───────────────────────────────────────

def extract_file_info(message) -> tuple[str | None, str | None, str | None]:
    """
    Inspect a telegram.Message and return (file_id, media_type, sticker_format).

    media_type  : "image" | "video"
    sticker_format : "static" | "video"
    Returns (None, None, None) when no recognisable media is found.
    """
    if message.sticker:
        fmt = "video" if message.sticker.is_video else "static"
        return message.sticker.file_id, "sticker", fmt
    if message.photo:
        return message.photo[-1].file_id, "image", "static"
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("image/"):
            return message.document.file_id, "image", "static"
        if mime.startswith("video/") or mime == "image/gif":
            return message.document.file_id, "video", "video"
        return message.document.file_id, "image", "static"
    if message.video:
        return message.video.file_id, "video", "video"
    if message.animation:
        return message.animation.file_id, "video", "video"
    if message.video_note:
        return message.video_note.file_id, "video", "video"
    return None, None, None


# ── Download helper ───────────────────────────────────────────

async def download_file_bytes(bot, file_id: str) -> io.BytesIO | None:
    """Download a Telegram file into a BytesIO buffer."""
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        return buf
    except Exception as exc:
        logger.error("Error downloading file %s: %s", file_id, exc)
        return None


# ── Static image pipeline ─────────────────────────────────────

def convert_to_sticker(file_bytes: io.BytesIO) -> io.BytesIO | None:
    """
    Convert any static image to a Telegram-compatible WEBP sticker.

    Rules:
      • RGBA mode
      • Longest side = 512 px (aspect ratio preserved)
      • File size ≤ 64 KB (quality stepped down if needed)
    """
    try:
        img = Image.open(file_bytes)
    except Exception:
        return None

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    max_dim = 512
    w, h = img.size
    if w > h:
        new_w, new_h = max_dim, int(h * max_dim / w)
    else:
        new_w, new_h = int(w * max_dim / h), max_dim

    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, new_h), Image.LANCZOS)

    for quality in [80, 60, 40, 20]:
        output = io.BytesIO()
        img.save(output, format="WEBP", quality=quality)
        if output.tell() <= 64_000:
            output.seek(0)
            return output

    output.seek(0)
    return output


async def async_convert_to_sticker(file_bytes: io.BytesIO) -> io.BytesIO | None:
    """Async wrapper: runs convert_to_sticker in a thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, convert_to_sticker, file_bytes)


# ── Video / GIF pipeline ──────────────────────────────────────

def convert_video_to_sticker(file_bytes: io.BytesIO) -> io.BytesIO | None:
    """
    Convert a video / GIF to a VP9 WEBM animated sticker.

    Rules:
      • Duration capped at 3 s
      • Longest side = 512 px (aspect ratio preserved)
      • Audio stripped
      • yuva420p pixel format (transparency support)
      • File size ≤ 256 KB (bitrate stepped down if needed)

    All temp files are cleaned up in a finally block regardless of outcome.
    """
    tmp_in_path = None
    tmp_out_path = None
    tmp_out_path2 = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
            tmp_in.write(file_bytes.getvalue())
            tmp_in_path = tmp_in.name

        tmp_out_path = tmp_in_path.replace(".mp4", "_out.webm")

        def _run_ffmpeg(bitrate: str, out_path: str) -> subprocess.CompletedProcess:
            cmd = [
                "ffmpeg", "-y", "-i", tmp_in_path,
                "-vf", "scale='if(gt(iw,ih),512,-2)':'if(gt(iw,ih),-2,512)',fps=30",
                "-c:v", "libvpx-vp9",
                "-b:v", bitrate,
                "-t", "3",
                "-an",
                "-pix_fmt", "yuva420p",
                out_path,
            ]
            return subprocess.run(cmd, capture_output=True, timeout=60)

        result = _run_ffmpeg("200k", tmp_out_path)

        if result.returncode != 0:
            logger.error("ffmpeg error: %s", result.stderr.decode()[:500])
            return None

        with open(tmp_out_path, "rb") as f:
            data = f.read()

        if len(data) > 256_000:
            tmp_out_path2 = tmp_in_path.replace(".mp4", "_out2.webm")
            _run_ffmpeg("100k", tmp_out_path2)
            if os.path.exists(tmp_out_path2):
                with open(tmp_out_path2, "rb") as f:
                    data = f.read()

        return io.BytesIO(data)

    except Exception as exc:
        logger.error("Video conversion error: %s", exc)
        return None
    finally:
        for path in (tmp_in_path, tmp_out_path, tmp_out_path2):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


async def async_convert_video_to_sticker(file_bytes: io.BytesIO) -> io.BytesIO | None:
    """Async wrapper: runs convert_video_to_sticker in a thread-pool executor.

    This prevents the synchronous ffmpeg subprocess call from blocking the
    asyncio event loop and delaying other bot updates.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, convert_video_to_sticker, file_bytes)


# ── Mask compositing ──────────────────────────────────────────

def apply_mask_to_image(
    source_bytes: io.BytesIO,
    mask_bytes: io.BytesIO,
    inverted: bool = False,
) -> io.BytesIO:
    """
    Composite a B&W mask onto a source image to produce a transparent cutout.

    White areas in the mask are *kept* by default; set inverted=True to flip.
    Returns a WEBP BytesIO (≤ 64 KB).
    """
    source = Image.open(source_bytes).convert("RGBA")
    mask = Image.open(mask_bytes).convert("L")
    mask = mask.resize(source.size, Image.LANCZOS)

    if inverted:
        mask = ImageOps.invert(mask)

    result = source.copy()
    result.putalpha(mask)

    max_dim = 512
    w, h = result.size
    if w > h:
        new_w, new_h = max_dim, int(h * max_dim / w)
    else:
        new_w, new_h = int(w * max_dim / h), max_dim
    result = result.resize((new_w, new_h), Image.LANCZOS)

    for quality in [80, 60, 40, 20]:
        output = io.BytesIO()
        result.save(output, format="WEBP", quality=quality)
        if output.tell() <= 64_000:
            output.seek(0)
            return output

    output.seek(0)
    return output


async def async_apply_mask_to_image(
    source_bytes: io.BytesIO,
    mask_bytes: io.BytesIO,
    inverted: bool = False,
) -> io.BytesIO:
    """Async wrapper: runs apply_mask_to_image in a thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, apply_mask_to_image, source_bytes, mask_bytes, inverted)



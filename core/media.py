"""Media-related pure helpers for wxauto messages."""

import hashlib
import os
import re
import time
from pathlib import Path

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
AI_COMPRESSED_IMAGE_DIRNAME = "compress_images"
AI_IMAGE_MAX_LONG_SIDE = 2048
AI_IMAGE_JPEG_QUALITY = 85
AI_IMAGE_LOSSLESS_SOURCE_FORMATS = {"PNG", "BMP", "GIF"}
WXAUTO_SAVE_CACHE_RETENTION_DAYS = 30


def is_image_path(value: str) -> bool:
    """Return True when value looks like an absolute local image path."""
    path = str(value or "").strip()
    if not path.lower().endswith(IMAGE_EXTENSIONS):
        return False
    pattern = re.compile(
        r"^("
        r"([A-Za-z]:[\\/])"
        r"|"
        r"(/[^/]+)"
        r")"
        r".+"
        r"\.(png|jpg|jpeg|gif|bmp|webp)$",
        re.IGNORECASE,
    )
    return bool(pattern.match(path))


def existing_local_image_path(value: str, download_dir: str) -> str:
    """Return a real image path only when it stays inside wxauto's download dir."""
    path = str(value or "").strip().strip('"').strip("'")
    if not path or "\n" in path or "\r" in path:
        return ""
    if not is_image_path(path):
        return ""
    try:
        abs_path = os.path.abspath(path)
        abs_download_dir = os.path.abspath(download_dir)
        if os.path.commonpath([abs_download_dir, abs_path]) != abs_download_dir:
            return ""
        return abs_path if os.path.isfile(abs_path) else ""
    except (OSError, ValueError):
        return ""


def image_content_hash(image_path: str) -> str:
    """Return a SHA1 hash for an image file, or empty string when unreadable."""
    path = str(image_path or "").strip()
    if not path:
        return ""
    try:
        digest = hashlib.sha1()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _ai_image_output_dir(image_path: str) -> str:
    path = Path(str(image_path or "")).resolve()
    for parent in [path.parent, *path.parents]:
        if parent.name.lower() == "wxauto_save":
            return str(parent / AI_COMPRESSED_IMAGE_DIRNAME)
    return str(path.parent / AI_COMPRESSED_IMAGE_DIRNAME)


def _image_has_transparency(image) -> bool:
    return image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)


def _ai_image_output_format(image) -> tuple[str, str]:
    source_format = str(getattr(image, "format", "") or "").upper()
    if _image_has_transparency(image) or source_format in AI_IMAGE_LOSSLESS_SOURCE_FORMATS:
        return "PNG", ".png"
    return "JPEG", ".jpg"


def prepare_ai_image_path(image_path: str) -> str:
    """Return a compressed image copy for AI APIs, preserving the original file."""
    path = str(image_path or "").strip()
    if not path or not os.path.isfile(path):
        return path
    try:
        if Path(path).parent.name == AI_COMPRESSED_IMAGE_DIRNAME:
            return path
    except Exception:
        return path

    digest = image_content_hash(path)
    if not digest:
        return path
    output_dir = _ai_image_output_dir(path)
    try:
        from PIL import Image, ImageOps

        os.makedirs(output_dir, exist_ok=True)
        with Image.open(path) as image:
            output_format, output_ext = _ai_image_output_format(image)
            output_path = os.path.join(output_dir, f"{digest}_{AI_IMAGE_MAX_LONG_SIDE}{output_ext}")
            if os.path.isfile(output_path):
                return output_path
            image = ImageOps.exif_transpose(image)
            image.thumbnail((AI_IMAGE_MAX_LONG_SIDE, AI_IMAGE_MAX_LONG_SIDE), Image.Resampling.LANCZOS)
            if output_format == "PNG":
                if image.mode not in ("RGB", "RGBA", "L"):
                    image = image.convert("RGBA" if _image_has_transparency(image) else "RGB")
                image.save(output_path, format="PNG", optimize=True)
            else:
                image = image.convert("RGB")
                image.save(
                    output_path,
                    format="JPEG",
                    quality=AI_IMAGE_JPEG_QUALITY,
                    optimize=True,
                    progressive=True,
                )
        return output_path if os.path.isfile(output_path) else path
    except Exception:
        return path


def cleanup_wxauto_save_cache(root_dir: str, *, retention_days: int = WXAUTO_SAVE_CACHE_RETENTION_DAYS) -> dict:
    """Delete files older than the retention window under wxauto_save."""
    root = Path(str(root_dir or "")).expanduser()
    if not root.name or root.name.lower() != "wxauto_save":
        return {"scanned_files": 0, "deleted_files": 0, "deleted_dirs": 0, "failed": 0, "skipped": True}
    try:
        root = root.resolve()
    except OSError:
        return {"scanned_files": 0, "deleted_files": 0, "deleted_dirs": 0, "failed": 1, "skipped": True}
    if not root.is_dir():
        return {"scanned_files": 0, "deleted_files": 0, "deleted_dirs": 0, "failed": 0, "skipped": True}

    retention_seconds = max(1, int(retention_days or WXAUTO_SAVE_CACHE_RETENTION_DAYS)) * 24 * 60 * 60
    cutoff = time.time() - retention_seconds
    stats = {"scanned_files": 0, "deleted_files": 0, "deleted_dirs": 0, "failed": 0, "skipped": False}

    for current_root, dirnames, filenames in os.walk(root, topdown=False):
        current_path = Path(current_root)
        for filename in filenames:
            file_path = current_path / filename
            try:
                stats["scanned_files"] += 1
                if file_path.stat().st_mtime >= cutoff:
                    continue
                file_path.unlink()
                stats["deleted_files"] += 1
            except OSError:
                stats["failed"] += 1

        if current_path == root:
            continue
        try:
            current_path.rmdir()
            stats["deleted_dirs"] += 1
        except OSError:
            pass

    return stats

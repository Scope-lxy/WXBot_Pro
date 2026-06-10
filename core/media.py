"""Media-related pure helpers for wxauto messages."""

import hashlib
import os
import re

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


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

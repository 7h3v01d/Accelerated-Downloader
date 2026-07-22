"""Pure, GUI-independent helpers for recognizing downloadable-looking URLs
(used by clipboard monitoring and drag-and-drop handling)."""
from __future__ import annotations

from urllib.parse import urlparse

DOWNLOADABLE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2",
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".iso", ".img", ".apk",
}


def is_probably_url(text: str) -> bool:
    text = (text or "").strip()
    if not text or " " in text or "\n" in text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def looks_like_download_url(text: str) -> bool:
    """Heuristic: is this clipboard/dropped text a URL pointing at something
    that's plausibly a file to download (as opposed to a regular webpage)?"""
    if not is_probably_url(text):
        return False
    path = urlparse(text).path.lower()
    return any(path.endswith(ext) for ext in DOWNLOADABLE_EXTENSIONS)


def extract_urls_from_mime_text(text: str) -> list[str]:
    """Splits multi-line dropped text (e.g. from a browser drag) into
    candidate URLs, preserving order and dropping blanks/duplicates."""
    seen = set()
    urls = []
    for line in (text or "").splitlines():
        candidate = line.strip()
        if candidate and is_probably_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls

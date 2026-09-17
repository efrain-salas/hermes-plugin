"""Deliver assistant-produced media to the mobile transcript.

Hermes never attaches files by itself: the agent points at them from its
response text with a ``MEDIA:/absolute/path`` tag, a bare absolute path, or a
remote ``![alt](https://…/image.png)`` markdown image. Messaging platforms
intercept those directives and send them natively; the mobile client cannot read
host paths, so this module mirrors that behaviour by copying (or downloading)
each referenced file into the profile attachment store and exposing it as the
same ``attachment`` content block the app already renders for user uploads.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp

from ..ids import new_id
from .validation import safe_filename

MAX_OUTBOUND_BYTES = 25 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 15
_MAX_FILENAME = 180

_EXECUTABLE_MAGIC = (
    b"MZ",
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
)

MIME_BY_EXTENSION: dict[str, str] = {
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    # Documents and data
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".log": "text/plain",
    ".rtf": "application/rtf",
    ".epub": "application/epub+zip",
    ".html": "text/html",
    ".htm": "text/html",
    # Audio and video
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    # Archives
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
}

_DELIVERABLE_EXTENSIONS = tuple(
    sorted((ext.lstrip(".") for ext in MIME_BY_EXTENSION), key=len, reverse=True)
)
_EXT_ALTERNATION = "|".join(_DELIVERABLE_EXTENSIONS)

_ENDS_PUNCTUATION = r"\)\]\}>,;:!?\"'*_`"

_MEDIA_PATH_PATTERN = (
    r"(?P<path>(?:~/|/|[A-Za-z]:[\\/])"
    r"(?:[^\s/\\`\"'*()\[\]{}<>]+[\\/])*"
    r"[^\s/\\`\"'*()\[\]{}<>]*\.(?:" + _EXT_ALTERNATION + r"))"
)

MEDIA_TAG_RE = re.compile(
    r"[`\"'*]{0,3}MEDIA:\s*" + _MEDIA_PATH_PATTERN + r"[`\"'*]{0,3}",
    re.IGNORECASE,
)
BARE_PATH_RE = re.compile(
    r"(?<![\w/])" + _MEDIA_PATH_PATTERN + r"(?![\w])",
    re.IGNORECASE,
)
MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?P<url>https?://[^\s)]+)\s*\)",
    re.IGNORECASE,
)
MARKDOWN_LOCAL_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*" + _MEDIA_PATH_PATTERN + r"\s*\)",
    re.IGNORECASE,
)
HTML_IMAGE_RE = re.compile(
    r"<img\s+[^>]*src=[\"']?(?P<url>https?://[^\s\"'<>]+)",
    re.IGNORECASE,
)

# Fenced examples in docs/comments must never be delivered as files.
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

# Credential / system surfaces an injected ``MEDIA:`` tag must never exfiltrate.
_DENIED_ROOTS = (
    Path("/etc"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/boot"),
)


@dataclass(frozen=True)
class MediaCandidate:
    kind: str  # "path" | "url"
    value: str
    start: int
    end: int


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def find_media_candidates(text: str) -> list[MediaCandidate]:
    """Locate assistant media directives in ``text`` (order preserved, no overlaps)."""
    if not text:
        return []
    candidates: list[MediaCandidate] = []
    reserved: list[tuple[int, int]] = [
        match.span() for match in _FENCED_CODE_RE.finditer(text)
    ]

    for match in MEDIA_TAG_RE.finditer(text):
        if _overlaps(match.span(), reserved):
            continue
        candidates.append(
            MediaCandidate("path", match.group("path").strip(), match.start(), match.end())
        )
        reserved.append(match.span())

    for match in MARKDOWN_LOCAL_IMAGE_RE.finditer(text):
        if _overlaps(match.span(), reserved):
            continue
        candidates.append(
            MediaCandidate("path", match.group("path"), match.start(), match.end())
        )
        reserved.append(match.span())

    for pattern in (MARKDOWN_IMAGE_RE, HTML_IMAGE_RE):
        for match in pattern.finditer(text):
            if _overlaps(match.span(), reserved):
                continue
            candidates.append(
                MediaCandidate("url", match.group("url"), match.start(), match.end())
            )
            reserved.append(match.span())

    for match in BARE_PATH_RE.finditer(text):
        if _overlaps(match.span(), reserved):
            continue
        value = match.group("path").rstrip(_ENDS_PUNCTUATION)
        if not value:
            continue
        candidates.append(MediaCandidate("path", value, match.start(), match.end()))
        reserved.append(match.span())

    candidates.sort(key=lambda item: item.start)
    return candidates


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    cleaned = "".join(kept)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _client_key(conversation_id: str, candidate: MediaCandidate) -> str:
    digest = hashlib.sha256(
        f"{conversation_id}\0{candidate.kind}\0{candidate.value}".encode("utf-8")
    ).hexdigest()
    return f"agent-{digest}"


def _validate_local_path(raw: str) -> Optional[Path]:
    try:
        expanded = Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    if not expanded.is_absolute():
        return None
    validator = _load_delivery_validator()
    if validator is not None:
        safe = validator(str(expanded))
        return Path(safe) if safe else None
    return _fallback_validate(expanded)


_delivery_validator: Callable[[str], Optional[str]] | None = None
_delivery_validator_loaded = False


def _load_delivery_validator() -> Callable[[str], Optional[str]] | None:
    """Reuse the core's delivery policy when the plugin runs inside Hermes."""
    global _delivery_validator, _delivery_validator_loaded
    if _delivery_validator_loaded:
        return _delivery_validator
    _delivery_validator_loaded = True
    try:
        from gateway.platforms.base import validate_media_delivery_path

        _delivery_validator = validate_media_delivery_path
    except Exception:  # noqa: BLE001 - standalone plugin installs have no gateway
        _delivery_validator = None
    return _delivery_validator


def _fallback_validate(path: Path) -> Optional[Path]:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    home = Path.home()
    denied = (
        *_DENIED_ROOTS,
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".config" / "hermes-plugin",
    )
    for root in denied:
        if resolved == root or root in resolved.parents:
            return None
    if resolved.suffix.lower() not in MIME_BY_EXTENSION:
        return None
    return resolved


def _detect_mime(data: bytes, filename: str) -> Optional[str]:
    if any(data.startswith(magic) for magic in _EXECUTABLE_MAGIC):
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return MIME_BY_EXTENSION.get(Path(filename).suffix.lower())


def _read_local(raw: str) -> Optional[tuple[bytes, str, str]]:
    path = _validate_local_path(raw)
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_OUTBOUND_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    mime = _detect_mime(data, path.name)
    if mime is None:
        return None
    return data, safe_filename(path.name), mime


Downloader = Callable[[str], Awaitable[Optional[tuple[bytes, str, str]]]]


async def _download_remote(url: str) -> Optional[tuple[bytes, str, str]]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return None
                declared = (
                    (response.headers.get("Content-Type") or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                data = await response.content.read(MAX_OUTBOUND_BYTES + 1)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None
    if len(data) > MAX_OUTBOUND_BYTES or not data:
        return None
    name = safe_filename(Path(parsed.path).name or "imagen")
    mime = _detect_mime(data, name)
    if declared.startswith("image/"):
        mime = declared if mime is None or mime == "application/octet-stream" else mime
    if mime is None or not mime.startswith("image/"):
        return None
    if "." not in name:
        name = safe_filename(name + (mimetypes.guess_extension(mime) or ".png"))
    return data, name, mime


def _write_original(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    with path.open("wb") as handle:
        handle.write(data)
    path.chmod(0o600)


async def _register(
    store: Any,
    conversation_id: str,
    candidate: MediaCandidate,
    downloader: Downloader | None,
) -> Optional[dict[str, Any]]:
    key = _client_key(conversation_id, candidate)
    existing = await asyncio.to_thread(store.attachment_by_client_id, key)
    if existing:
        if existing.get("status") == "deleted":
            return None
        return _block_from_row(existing)

    if candidate.kind == "path":
        fetched = await asyncio.to_thread(_read_local, candidate.value)
    elif downloader is not None:
        fetched = await downloader(candidate.value)
    else:
        fetched = await _download_remote(candidate.value)
    if fetched is None:
        return None
    data, filename, mime = fetched

    attachment_id = new_id("att")
    final = store.files_root / "originals" / attachment_id / "file"
    try:
        await asyncio.to_thread(_write_original, final, data)
    except OSError:
        return None
    row = await asyncio.to_thread(
        store.create_attachment,
        {
            "public_id": attachment_id,
            "conversation_id": conversation_id,
            "client_attachment_id": key,
            "filename": filename[:_MAX_FILENAME],
            "safe_filename": filename[:_MAX_FILENAME],
            "mime_type": mime,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "storage_path": str(final),
        },
    )
    await asyncio.to_thread(store.update_attachment, row["public_id"], "ready")
    return _block_from_row({**row, "status": "ready"})


def _block_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "attachment",
        "attachment_id": row.get("public_id"),
        "filename": row.get("filename"),
        "mime_type": row.get("mime_type"),
        "status": row.get("status"),
    }


async def collect_outbound_media(
    store: Any,
    conversation_id: str,
    text: str,
    *,
    downloader: Downloader | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(clean_text, attachment_blocks)`` for an assistant message."""
    candidates = find_media_candidates(text)
    if not candidates:
        return text, []
    removed: list[tuple[int, int]] = []
    blocks: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            block = await _register(store, conversation_id, candidate, downloader)
        except Exception:  # noqa: BLE001 - never fail the transcript on one bad ref
            block = None
        if block is None:
            continue
        removed.append((candidate.start, candidate.end))
        blocks.append(block)
    if not blocks:
        return text, []
    return _strip_spans(text, removed), blocks

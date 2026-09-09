from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from ..api.errors import MobileError
from ..constants import SUPPORTED_MIME_TYPES

_EXECUTABLE_MAGIC = (
    b"MZ",
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
)


def safe_filename(value: str) -> str:
    name = Path(value or "file").name.replace("\x00", "")
    name = re.sub(r"[^\w.() -]+", "_", name, flags=re.UNICODE).strip(" .")
    return (name or "file")[:180]


def detect_mime(head: bytes, filename: str, declared: str | None) -> str:
    if any(head.startswith(magic) for magic in _EXECUTABLE_MAGIC):
        raise MobileError(
            "unsupported_file_type",
            "Los archivos ejecutables no están permitidos.",
            415,
        )
    if head.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif head.startswith(b"PK\x03\x04") and filename.lower().endswith(".docx"):
        detected = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    else:
        guessed = mimetypes.guess_type(filename)[0]
        detected = (
            guessed
            or (declared or "application/octet-stream").split(";", 1)[0].strip().lower()
        )
        if detected in {"text/x-markdown", "text/md"}:
            detected = "text/markdown"
        if filename.lower().endswith(".py"):
            detected = "text/x-python"
        elif filename.lower().endswith((".js", ".mjs", ".ts", ".tsx")):
            detected = "text/javascript"
    if detected not in SUPPORTED_MIME_TYPES:
        raise MobileError(
            "unsupported_file_type", "El tipo de archivo no está permitido.", 415
        )
    return detected

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..persistence.repositories import ProfileStore

SCHEMA = {
    "name": "mobile_attachment_read",
    "description": "Read a bounded page of user-provided mobile attachment text by public attachment ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "attachment_id": {"type": "string"},
            "conversation_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20000,
                "default": 8000,
            },
        },
        "required": ["attachment_id", "conversation_id"],
    },
}


def read_attachment(args: dict[str, Any], **_: Any) -> str:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        store = ProfileStore(home)
        row = store.attachment(str(args.get("attachment_id") or ""))
        conversation_id = str(args.get("conversation_id") or "")
        if (
            not row
            or row.get("status") != "ready"
            or row.get("conversation_id") != conversation_id
        ):
            return json.dumps({"ok": False, "error": "attachment_not_found"})
        extracted = row.get("extracted_path")
        if not extracted:
            return json.dumps(
                {
                    "ok": True,
                    "mime_type": row["mime_type"],
                    "text": "",
                    "has_more": False,
                }
            )
        target = Path(extracted).resolve()
        allowed = (store.files_root / "extracted").resolve()
        if allowed not in target.parents or target.is_symlink():
            return json.dumps({"ok": False, "error": "storage_unavailable"})
        offset = max(0, int(args.get("offset", 0)))
        max_chars = min(20_000, max(1, int(args.get("max_chars", 8000))))
        text = target.read_text(encoding="utf-8", errors="replace")
        return json.dumps(
            {
                "ok": True,
                "attachment_id": row["public_id"],
                "offset": offset,
                "text": text[offset : offset + max_chars],
                "has_more": offset + max_chars < len(text),
            },
            ensure_ascii=False,
        )
    except Exception:
        return json.dumps({"ok": False, "error": "storage_unavailable"})

from __future__ import annotations

import re
import secrets

_ID_RE = re.compile(r"^[a-z]{3,5}_[A-Za-z0-9_-]{8,128}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def valid_request_id(value: str) -> bool:
    return bool(re.fullmatch(r"req_[A-Za-z0-9_-]{8,128}", value or ""))


def valid_public_id(value: str, prefix: str) -> bool:
    return bool(_ID_RE.fullmatch(value or "")) and value.startswith(prefix + "_")

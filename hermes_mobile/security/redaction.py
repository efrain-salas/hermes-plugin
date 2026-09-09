from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:access|refresh|pairing|push)_token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(API_SERVER_KEY\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"ExponentPushToken\[[^\]]+\]"),
)


def redact(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text
        )
    return text[:1000]

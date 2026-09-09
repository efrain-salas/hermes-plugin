from __future__ import annotations

API_VERSION = "1.0"
PLUGIN_ID = "hermes-mobile"
DEFAULT_SCOPES = (
    "conversations:read",
    "conversations:write",
    "runs:write",
    "approvals:write",
    "attachments:read",
    "attachments:write",
    "devices:self",
)
ALL_SCOPES = DEFAULT_SCOPES + ("devices:manage",)
SUPPORTED_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "text/x-python",
    "text/javascript",
    "application/javascript",
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

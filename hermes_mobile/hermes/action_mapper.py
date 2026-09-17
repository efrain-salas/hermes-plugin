from __future__ import annotations

from typing import Any

from ..ids import new_id
from ..security.redaction import redact

# Version of the ``action`` payload attached to run events. Clients branch on it.
ACTION_SCHEMA = 1

# Curated tool -> kind table. Unknown tools fall back to ``mcp_tool``/``tool``;
# the kind is a stable contract shared with clients (see README).
_TOOL_KINDS: dict[str, str] = {
    "web_search": "web_search",
    "search_files": "search_files",
    "session_search": "search_sessions",
    "web_extract": "web_extract",
    "browser_navigate": "browse",
    "browser_click": "browse",
    "browser_type": "browse",
    "browser_exec": "browse",
    "computer_use": "browse",
    "read_file": "file_read",
    "write_file": "file_write",
    "patch": "file_edit",
    "terminal": "shell",
    "execute_code": "code",
    "process_manage": "process",
    "image_generate": "image_generate",
    "video_generate": "video_generate",
    "text_to_speech": "speech",
    "vision_analyze": "vision",
    "memory": "memory",
    "todo_list": "todo",
    "skill_view": "skill_read",
    "skills_list": "skill_list",
    "skill_manage": "skill_edit",
    "delegate_task": "subagent",
    "cronjob_manage": "schedule",
    "clarify": "clarify",
    "send_message": "send_message",
    "mobile_attachment_read": "attachment",
    "tool_describe": "tool_info",
    "tool_search": "tool_info",
}

# Best-effort semantic type of ``action.detail`` so clients can format it
# (e.g. show the host of a URL, wrap a command in monospace, ...).
_KIND_TARGETS: dict[str, str] = {
    "web_search": "query",
    "web_extract": "url",
    "browse": "url",
    "file_read": "path",
    "file_write": "path",
    "file_edit": "path",
    "attachment": "path",
    "search_files": "pattern",
    "shell": "command",
    "code": "command",
    "skill_read": "name",
    "skill_edit": "name",
    "skill_list": "category",
    "subagent": "goal",
    "schedule": "action",
    "vision": "text",
    "clarify": "text",
    "memory": "text",
    "send_message": "text",
    "tool_info": "text",
    "mcp_tool": "text",
    "tool": "text",
    "other": "text",
}

_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "tool": ("preview", "summary", "output_preview", "command"),
    "subagent": ("goal", "summary", "preview"),
    "approval": ("command", "action", "title", "summary"),
}


def classify_kind(tool: str | None) -> str:
    """Map a raw tool name to the shared action ``kind``."""
    if not tool:
        return "other"
    if tool in _TOOL_KINDS:
        return _TOOL_KINDS[tool]
    if tool.startswith(("mcp__", "mcp_")):
        return "mcp_tool"
    return "tool"


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return redact(value)
    return None


def _failed(data: dict[str, Any]) -> bool:
    for key in ("error", "is_error", "failed"):
        value = data.get(key)
        if isinstance(value, bool):
            if value:
                return True
        elif value:
            return True
    return False


def _target(kind: str, detail: str | None) -> dict[str, str] | None:
    target_type = _KIND_TARGETS.get(kind)
    if not detail or not target_type:
        return None
    return {"type": target_type, "value": detail}


def _duration_ms(data: dict[str, Any]) -> int | None:
    value = data.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(float(value) * 1000))


def _action(
    *,
    kind: str,
    status: str,
    detail: str | None,
    action_id: str | None,
    data: dict[str, Any],
    tool: str | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "schema": ACTION_SCHEMA,
        "kind": kind,
        "status": status,
        "detail": detail,
    }
    if action_id:
        action["id"] = action_id
    if tool:
        action["tool"] = tool
    target = _target(kind, detail)
    if target:
        action["target"] = target
    duration = _duration_ms(data)
    if duration is not None:
        action["duration_ms"] = duration
    if _failed(data):
        action["failed"] = True
    return action


class ActionTracker:
    """Pairs ``*.started`` with ``*.completed`` by sharing one action id.

    Live events carry no ``tool_call_id`` (Hermes omits it on the progress
    stream), and completions can arrive in a different order than starts, so
    each tool name keeps a FIFO queue of open actions.
    """

    def __init__(self) -> None:
        self._open: dict[str, list[str]] = {}

    def start(self, key: str | None) -> str:
        action_id = new_id("act")
        self._open.setdefault(key or "action", []).append(action_id)
        return action_id

    def finish(self, key: str | None) -> str:
        queue = self._open.get(key or "action")
        if queue:
            return queue.pop(0)
        return new_id("act")


def build_action(
    event_type: str,
    data: dict[str, Any],
    tracker: ActionTracker | None = None,
) -> dict[str, Any] | None:
    """Build the normalized ``action`` for an event, or ``None`` for lifecycle.

    Accepts both wire dialects seen in production: native full runs expose
    ``tool``/``error`` while in-process quick runs expose ``tool_name``/``is_error``.
    """
    if event_type.startswith("tool."):
        tool = _first_text(data, ("tool", "tool_name", "name"))
        key = tool or "tool"
        if event_type == "tool.started":
            action_id = tracker.start(key) if tracker else new_id("act")
            status = "started"
        else:
            action_id = tracker.finish(key) if tracker else new_id("act")
            status = (
                "failed" if event_type.endswith(".failed") or _failed(data) else "completed"
            )
        return _action(
            kind=classify_kind(tool),
            status=status,
            detail=_first_text(data, _DETAIL_KEYS["tool"]),
            action_id=action_id,
            data=data,
            tool=tool,
        )

    if event_type.startswith("subagent."):
        started = event_type.endswith(".started")
        action_id = (
            tracker.start("subagent") if started else tracker.finish("subagent")
        ) if tracker else new_id("act")
        action = _action(
            kind="subagent",
            status="started" if started else "completed",
            detail=_first_text(data, _DETAIL_KEYS["subagent"]),
            action_id=action_id,
            data=data,
            tool="delegate_task",
        )
        subagent_id = data.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id:
            action["subagent_id"] = subagent_id
        return action

    if event_type.startswith("approval."):
        action = _action(
            kind="approval",
            status="pending" if event_type.endswith(".requested") else "resolved",
            detail=_first_text(data, _DETAIL_KEYS["approval"]),
            action_id=None,
            data=data,
        )
        for key in ("approval_id", "request_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                action["approval_id"] = value
                break
        return action

    return None


# Fields copied onto a merged action from any of its events. ``detail`` is kept
# from the start event (completions carry no preview); ``duration_ms``/``failed``
# only exist on the terminal event.
_MERGE_FIELDS = (
    "kind",
    "tool",
    "detail",
    "target",
    "duration_ms",
    "failed",
    "subagent_id",
    "approval_id",
)


def merge_run_actions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a run's action events into one entry per started/completed pair.

    ``events`` are stored run events (``data.action`` may be missing on lifecycle
    events, which are skipped). Output keeps first-seen order and merges later
    facts (status, duration, failure) over the earlier ones.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        action = (event.get("data") or {}).get("action")
        if not isinstance(action, dict):
            continue
        key = action.get("id") or event.get("event_id")
        if key not in merged:
            merged[key] = {
                "id": key,
                "created_at": event.get("created_at"),
                "sequence": event.get("sequence"),
            }
            order.append(key)
        entry = merged[key]
        for field in _MERGE_FIELDS:
            if action.get(field) is not None:
                entry[field] = action[field]
        entry["status"] = action.get("status") or entry.get("status")
    return [merged[key] for key in order]


def attach_transcript_inputs(
    actions: list[dict[str, Any]], tool_calls: list[dict[str, Any]]
) -> None:
    """Attach full ``input``/``output_preview`` from the transcript, in place.

    Live events carry no tool arguments, but the native message transcript keeps
    every ``tool_calls[].function.arguments``. Pairing is by tool name + FIFO
    order (the stream omits ``tool_call_id``); subagent/approval rows are skipped.
    """
    queues: dict[str, list[dict[str, Any]]] = {}
    for call in tool_calls:
        name = call.get("name")
        queues.setdefault(name, []).append(call)
    for action in actions:
        if action.get("kind") in {"subagent", "approval"}:
            continue
        queue = queues.get(action.get("tool"))
        if not queue:
            continue
        call = queue.pop(0)
        if call.get("input") is not None:
            action["input"] = call["input"]
        if call.get("output_preview") is not None:
            action["output_preview"] = call["output_preview"]


from __future__ import annotations

from typing import Any

_EVENT_NAMES = {
    "run.queued": "run.queued",
    "run.started": "run.started",
    "reasoning.started": "reasoning.started",
    "reasoning.available": "reasoning.available",
    "message.delta": "message.delta",
    "assistant.delta": "message.delta",
    "tool.started": "tool.started",
    "tool.completed": "tool.completed",
    "tool.failed": "tool.failed",
    "subagent.start": "subagent.started",
    "subagent.started": "subagent.started",
    "subagent.complete": "subagent.completed",
    "subagent.completed": "subagent.completed",
    "approval.request": "approval.requested",
    "approval.requested": "approval.requested",
    "approval.responded": "approval.resolved",
    "assistant.completed": "message.completed",
    "message.completed": "message.completed",
    "run.completed": "run.completed",
    "run.failed": "run.failed",
    "run.cancelled": "run.cancelled",
}


def map_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    source_type = str(event.get("event") or event.get("type") or "")
    target = _EVENT_NAMES.get(source_type)
    if not target:
        return None
    blocked = {
        "event",
        "type",
        "run_id",
        "timestamp",
        "chain_of_thought",
        "reasoning_content",
    }
    data = {key: value for key, value in event.items() if key not in blocked}
    if target == "reasoning.available":
        data = {"summary": data.get("text") or data.get("summary") or ""}
    return target, data

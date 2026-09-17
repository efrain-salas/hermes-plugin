from __future__ import annotations

from hermes_mobile.hermes.action_mapper import (
    ACTION_SCHEMA,
    ActionTracker,
    attach_transcript_inputs,
    build_action,
    classify_kind,
    merge_run_actions,
)


def test_native_tool_started_becomes_a_search_action() -> None:
    action = build_action(
        "tool.started",
        {"tool": "web_search", "preview": "site:jw.org/es curso de la Biblia"},
    )
    assert action is not None
    assert action["schema"] == ACTION_SCHEMA
    assert action["kind"] == "web_search"
    assert action["status"] == "started"
    assert action["tool"] == "web_search"
    assert action["detail"] == "site:jw.org/es curso de la Biblia"
    assert action["target"] == {
        "type": "query",
        "value": "site:jw.org/es curso de la Biblia",
    }
    assert action["id"].startswith("act_")


def test_tracker_pairs_started_and_completed_with_one_id() -> None:
    tracker = ActionTracker()
    started = build_action(
        "tool.started", {"tool": "web_search", "preview": "clima"}, tracker
    )
    completed = build_action(
        "tool.completed",
        {"tool": "web_search", "duration": 2.96, "error": False},
        tracker,
    )
    assert started is not None and completed is not None
    assert started["id"] == completed["id"]
    assert completed["status"] == "completed"
    assert completed["duration_ms"] == 2960
    assert "failed" not in completed


def test_quick_dialect_is_normalized() -> None:
    action = build_action(
        "tool.completed",
        {"tool_name": "web_search", "duration": 1.52, "is_error": True},
    )
    assert action is not None
    assert action["kind"] == "web_search"
    assert action["tool"] == "web_search"
    assert action["status"] == "failed"
    assert action["failed"] is True
    assert action["duration_ms"] == 1520


def test_unknown_and_mcp_tools_degrade_gracefully() -> None:
    unknown = build_action("tool.started", {"tool": "future_tool", "preview": None})
    assert unknown is not None
    assert unknown["kind"] == "tool"
    assert unknown["detail"] is None
    assert "target" not in unknown

    mcp = build_action(
        "tool.started",
        {"tool": "mcp__whatsapp_readonly__chats_list", "preview": None},
    )
    assert mcp is not None
    assert mcp["kind"] == "mcp_tool"


def test_kind_specific_targets() -> None:
    read = build_action("tool.started", {"tool": "read_file", "preview": "tool.py L1-180"})
    assert read is not None and read["target"] == {"type": "path", "value": "tool.py L1-180"}

    shell = build_action(
        "tool.started", {"tool": "terminal", "preview": "docker ps -a"}
    )
    assert shell is not None and shell["target"] == {"type": "command", "value": "docker ps -a"}

    extract = build_action(
        "tool.started", {"tool": "web_extract", "preview": "https://www.apple.com/es/"}
    )
    assert extract is not None and extract["target"] == {
        "type": "url",
        "value": "https://www.apple.com/es/",
    }


def test_subagent_and_approval_actions() -> None:
    tracker = ActionTracker()
    started = build_action(
        "subagent.started",
        {"goal": "investigate the report", "subagent_id": "sub_1"},
        tracker,
    )
    completed = build_action("subagent.completed", {"summary": "done"}, tracker)
    assert started is not None and completed is not None
    assert started["kind"] == "subagent"
    assert started["tool"] == "delegate_task"
    assert started["subagent_id"] == "sub_1"
    assert started["id"] == completed["id"]
    assert completed["status"] == "completed"

    pending = build_action(
        "approval.requested",
        {"command": "rm -rf /tmp/x", "approval_id": "appr_1"},
    )
    assert pending is not None
    assert pending["kind"] == "approval"
    assert pending["status"] == "pending"
    assert pending["detail"] == "rm -rf /tmp/x"
    assert pending["approval_id"] == "appr_1"

    resolved = build_action("approval.resolved", {"choice": "allow_once"})
    assert resolved is not None and resolved["status"] == "resolved"


def test_secrets_are_redacted_and_lifecycle_events_have_no_action() -> None:
    action = build_action(
        "tool.started",
        {"tool": "terminal", "preview": "curl -H 'authorization: bearer sk-live-123'"},
    )
    assert action is not None
    assert "sk-live-123" not in action["detail"]
    assert "[REDACTED]" in action["detail"]

    for event_type in ("run.queued", "run.started", "message.delta", "reasoning.available"):
        assert build_action(event_type, {"text": "x"}) is None


def test_classify_kind_fallbacks() -> None:
    assert classify_kind("patch") == "file_edit"
    assert classify_kind("mcp_custom") == "mcp_tool"
    assert classify_kind("whatever") == "tool"
    assert classify_kind(None) == "other"


def test_merge_run_actions_pairs_and_preserves_order() -> None:
    tracker = ActionTracker()
    started = build_action(
        "tool.started", {"tool": "web_search", "preview": "clima"}, tracker
    )
    completed = build_action(
        "tool.completed", {"tool": "web_search", "duration": 2.5, "error": False}, tracker
    )
    events = [
        {"event_id": "e1", "sequence": 1, "data": {"delta": "x"}},
        {"event_id": "e2", "sequence": 2, "created_at": "t2", "data": {"action": started}},
        {"event_id": "e3", "sequence": 3, "data": {"summary": "no action here"}},
        {"event_id": "e4", "sequence": 4, "created_at": "t4", "data": {"action": completed}},
    ]
    merged = merge_run_actions(events)
    assert len(merged) == 1
    entry = merged[0]
    assert entry["id"] == started["id"]
    assert entry["status"] == "completed"
    assert entry["detail"] == "clima"
    assert entry["duration_ms"] == 2500
    assert entry["created_at"] == "t2"
    assert "input" not in entry


def test_attach_transcript_inputs_matches_by_name_fifo() -> None:
    actions = [
        {"kind": "web_search", "tool": "web_search"},
        {"kind": "web_search", "tool": "web_search"},
        {"kind": "subagent", "tool": "delegate_task"},
    ]
    calls = [
        {"name": "web_search", "input": {"query": "one"}},
        {"name": "web_search", "input": {"query": "two"}},
    ]
    attach_transcript_inputs(actions, calls)
    assert actions[0]["input"] == {"query": "one"}
    assert actions[1]["input"] == {"query": "two"}
    assert "input" not in actions[2]


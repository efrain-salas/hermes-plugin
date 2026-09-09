from hermes_mobile.hermes.event_mapper import map_event


def test_event_mapping_drops_internal_reasoning():
    event_type, data = map_event(
        {
            "event": "reasoning.available",
            "text": "safe",
            "reasoning_content": "private chain",
        }
    )
    assert event_type == "reasoning.available"
    assert data == {"summary": "safe"}


def test_unknown_event_is_ignored_and_subagents_are_normalized():
    assert map_event({"event": "future.event"}) is None
    assert map_event({"event": "subagent.start", "task_id": "x"}) == (
        "subagent.started",
        {"task_id": "x"},
    )

from __future__ import annotations

import os
import sys
import types
from contextlib import contextmanager
from datetime import UTC, datetime

from hermes_mobile.hermes.cron_reader import NativeCronReader, extract_response


def test_extract_response_returns_only_final_response_section():
    transcript = (
        "# Cron Job: Informe\n"
        "\n"
        "**Job ID:** job123\n"
        "\n"
        "## Prompt\n"
        "\n"
        "## Your previous run's output\n"
        "\n"
        "```\n"
        "## Response\n"
        "una respuesta anterior\n"
        "```\n"
        "\n"
        "## Script Output\n"
        "\n"
        "```\n"
        '{"wakeAgent": true}\n'
        "```\n"
        "\n"
        "## Response\n"
        "\n"
        "## Grupo 4 — 11 de septiembre\n"
        "\n"
        "Sin novedades importantes hoy.\n"
    )
    assert extract_response(transcript) == (
        "## Grupo 4 — 11 de septiembre\n\nSin novedades importantes hoy."
    )


def test_extract_response_without_response_returns_none():
    transcript = (
        "# Cron Job: Mantener WAHA activo\n"
        "\n"
        "**Status:** silent (empty output)\n"
    )
    assert extract_response(transcript) is None
    assert extract_response("## Response\n\n   \n") is None


def test_native_cron_reader_delegates_to_hermes_and_reads_native_output(
    tmp_path, monkeypatch
):
    calls = []
    job = {"id": "job123", "attach_to_session": True}
    execution = {
        "id": "exec123",
        "job_id": "job123",
        "finished_at": "2026-09-10T08:00:02Z",
    }

    jobs_module = types.ModuleType("cron.jobs")

    @contextmanager
    def use_cron_store(home):
        calls.append(("scope", home))
        yield

    def update_job(job_id, updates):
        calls.append(("update", job_id, updates))
        job.update(updates)
        return job

    jobs_module.use_cron_store = use_cron_store
    jobs_module.update_job = update_job
    cron_module = types.ModuleType("cron")
    cron_module.jobs = jobs_module

    executions_module = types.ModuleType("cron.executions")
    executions_module.list_executions = lambda **kwargs: [
        {**execution, "query": kwargs}
    ]
    executions_module.get_execution = lambda execution_id: (
        execution if execution_id == "exec123" else None
    )

    constants_module = types.ModuleType("hermes_constants")
    constants_module.set_hermes_home_override = lambda path: (
        calls.append(("home", path)) or "token"
    )
    constants_module.reset_hermes_home_override = lambda token: calls.append(
        ("reset", token)
    )

    appended = []

    class SessionDB:
        def __init__(self, db_path):
            appended.append(("open", db_path))

        def append_message(self, session_id, role, content):
            appended.append((session_id, role, content))

        def close(self):
            appended.append(("close",))

    state_module = types.ModuleType("hermes_state")
    state_module.SessionDB = SessionDB
    monkeypatch.setitem(sys.modules, "cron", cron_module)
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs_module)
    monkeypatch.setitem(sys.modules, "cron.executions", executions_module)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants_module)
    monkeypatch.setitem(sys.modules, "hermes_state", state_module)

    reader = NativeCronReader()
    rows = reader.list_executions(tmp_path, "job123", limit=7)
    assert rows[0]["query"] == {
        "job_id": "job123",
        "limit": 7,
        "before_claimed_at": None,
    }
    assert reader.get_execution(tmp_path, "exec123") == execution
    assert (
        reader.update_job_metadata(
            tmp_path,
            "job123",
            {"attach_to_session": False, "deliver": "telegram"},
        )["attach_to_session"]
        is False
    )
    assert reader.update_job_metadata(tmp_path, "job123", {"deliver": "local"}) is None
    assert calls.count(("reset", "token")) == 3

    output_dir = tmp_path / "cron" / "output" / "job123"
    output_dir.mkdir(parents=True)
    output = output_dir / "2026-09-10_08-00-00.md"
    output.write_text(
        "# Cron Job: Informe\n"
        "\n"
        "**Job ID:** job123\n"
        "**Run Time:** 2026-09-10 08:00:00\n"
        "\n"
        "## Prompt\n"
        "\n"
        "Resume los mensajes pendientes.\n"
        "\n"
        "## Response\n"
        "\n"
        "native result\n",
        encoding="utf-8",
    )
    target = datetime(2026, 9, 10, 8, 0, 2, tzinfo=UTC).timestamp()
    os.utime(output, (target - 1, target - 1))
    assert reader.execution_output(tmp_path, "job123", execution) == "native result"
    assert (
        reader.execution_output(
            tmp_path, "job123", {**execution, "finished_at": "invalid"}
        )
        is None
    )
    assert (
        reader.execution_output(tmp_path, "job123", {**execution, "finished_at": None})
        is None
    )

    reader.append_conversation_result(tmp_path, "session-1", "result")
    assert appended == [
        ("open", tmp_path / "state.db"),
        ("session-1", "user", "result"),
        ("close",),
    ]

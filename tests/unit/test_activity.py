from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from hermes_mobile.activity import ActivityPublisher, gateway_active_count


class FakeRunner:
    def __init__(self, **counts: Any):
        for name, value in counts.items():
            setattr(self, name, value)


def test_gateway_active_count_prefers_aggregate():
    runner = FakeRunner(
        _active_work_count=lambda: 3,
        _running_agent_count=lambda: 9,
    )
    assert gateway_active_count(runner) == 3


def test_gateway_active_count_falls_back_to_partials():
    runner = FakeRunner(
        _running_agent_count=lambda: 1,
        _active_cron_job_count=lambda: 2,
        _active_api_run_count=lambda: 4,
        _active_deferred_agent_worker_count=lambda: 0,
    )
    assert gateway_active_count(runner) == 7


def test_gateway_active_count_without_runner_is_unknown():
    assert gateway_active_count(None) is None


def test_gateway_active_count_survives_broken_probe():
    def boom() -> int:
        raise RuntimeError("probe failed")

    runner = FakeRunner(_active_work_count=boom, _running_agent_count=lambda: 2)
    assert gateway_active_count(runner) == 2


def test_publisher_tracks_runs_and_publishes(tmp_path: Path):
    path = tmp_path / "plugin-data" / "hermes-mobile" / "activity.json"
    publisher = ActivityPublisher(path, boot_id="boot_test")

    assert publisher.write() is True
    assert json.loads(path.read_text(encoding="utf-8"))["active_agents"] == 0

    publisher.begin("run-1", "default")
    publisher.begin("run-2", "default")
    publisher.begin("run-3", "secureauthv2")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["plugin_active_runs"] == 3
    assert snapshot["active_agents"] == 3
    assert snapshot["source"] == "plugin"
    assert snapshot["profiles"] == {"default": 2, "secureauthv2": 1}

    publisher.end("run-1")
    assert publisher.active_runs == 2


def test_publisher_merges_live_gateway_count(tmp_path: Path):
    path = tmp_path / "activity.json"
    publisher = ActivityPublisher(path, boot_id="boot_test")
    publisher.bind_gateway(FakeRunner(_active_work_count=lambda: 5))
    publisher.begin("run-1", "default")

    snapshot = publisher.snapshot()
    assert snapshot["gateway_active_agents"] == 5
    assert snapshot["plugin_active_runs"] == 1
    assert snapshot["active_agents"] == 5
    assert snapshot["source"] == "gateway"

    publisher.reset()
    assert publisher.active_runs == 0
    assert json.loads(path.read_text(encoding="utf-8"))["active_agents"] == 5


def test_publisher_ignores_blank_run_id(tmp_path: Path):
    publisher = ActivityPublisher(tmp_path / "activity.json", boot_id="boot_test")
    publisher.begin("", "default")
    assert publisher.active_runs == 0


class GatedFacade:
    def __init__(self):
        self.gate = asyncio.Event()

    async def start(self):
        return None

    async def close(self):
        return None

    async def stream_run_events(self, profile, run_id):
        yield {"event": "run.started", "run_id": run_id}
        await self.gate.wait()
        yield {"event": "run.completed", "run_id": run_id}


async def test_runtime_publishes_and_clears_activity(runtime, tmp_path: Path):
    runtime.facade = GatedFacade()  # type: ignore[assignment]
    await runtime.start()
    runtime.activity.bind_gateway(FakeRunner(_active_work_count=lambda: 1))

    runtime.mirror_run("default", "pub-run-1", "hermes-run-1")
    for _ in range(100):
        if runtime.activity.active_runs == 1:
            break
        await asyncio.sleep(0.01)
    assert runtime.activity.active_runs == 1

    published = json.loads(runtime.activity.path.read_text(encoding="utf-8"))
    assert published["gateway_active_agents"] == 1
    assert published["profiles"] == {"default": 1}

    runtime.facade.gate.set()  # type: ignore[attr-defined]
    for _ in range(100):
        if runtime.activity.active_runs == 0:
            break
        await asyncio.sleep(0.01)
    assert runtime.activity.active_runs == 0
    await runtime.close()

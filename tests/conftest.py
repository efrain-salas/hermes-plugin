from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_mobile.api.routes import MobileAPI
from hermes_mobile.config import MobileConfig, PushConfig
from hermes_mobile.runtime import MobileRuntime


class FakeFacade:
    def __init__(self):
        self.sessions: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.messages: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.executions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.outputs: dict[tuple[str, str], str] = {}
        self.mirrored_results: list[tuple[str, str]] = []
        self.created_bodies: list[tuple[str, dict[str, Any]]] = []
        self.model_updates: list[tuple[str, str, str, str | None]] = []
        self.preference_updates: list[dict[str, Any]] = []
        self.profile_preferences_data: dict[str, Any] = {
            "model": "mock-model",
            "provider": "mock",
            "reasoning_effort": "medium",
        }
        self.counter = 0
        self.fail = False

    async def start(self):
        return None

    async def close(self):
        return None

    def _guard(self):
        if self.fail:
            raise RuntimeError("secret API_SERVER_KEY=should-never-leak")

    async def capabilities(self, profile):
        self._guard()
        return {"features": {"run_submission": True}}

    async def list_conversations(self, profile, **params):
        self._guard()
        rows = list(self.sessions[profile].values())
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
        return {
            "data": rows[offset : offset + limit],
            "has_more": offset + limit < len(rows),
        }

    async def create_conversation(self, profile, body):
        self._guard()
        self.created_bodies.append((profile, body))
        self.counter += 1
        sid = f"internal-{profile}-{self.counter}"
        row = {
            "id": sid,
            "title": body.get("title"),
            "model": body.get("model") or "mock-model",
            "started_at": 1_788_948_000.0,
            "last_active": 1_788_948_000.0,
            "pinned": False,
            "archived": False,
            "message_count": 0,
        }
        self.sessions[profile][sid] = row
        return {"session": row}

    async def get_conversation(self, profile, session_id):
        self._guard()
        return {"session": self.sessions[profile][session_id]}

    async def update_conversation(self, profile, session_id, body):
        self._guard()
        self.sessions[profile][session_id].update(body)
        return {"session": self.sessions[profile][session_id]}

    async def set_conversation_model(
        self, profile, session_id, model, reasoning_effort=None
    ):
        self.model_updates.append((profile, session_id, model, reasoning_effort))
        self.sessions[profile][session_id]["model"] = model
        return {"session": self.sessions[profile][session_id]}

    async def delete_conversation(self, profile, session_id):
        self._guard()
        self.sessions[profile].pop(session_id)

    async def get_messages(self, profile, session_id, **params):
        self._guard()
        return {"data": self.messages[(profile, session_id)]}

    async def fork_conversation(self, profile, session_id, body):
        source = self.sessions[profile][session_id]
        return await self.create_conversation(
            profile, {"title": body.get("title") or f"{source['title']} fork"}
        )

    async def create_run(self, profile, body, idempotency_key):
        self._guard()
        for (p, rid), run in self.runs.items():
            if p == profile and run.get("idem") == idempotency_key:
                return {"run_id": rid, "status": run["status"]}
        self.counter += 1
        rid = f"hermes-run-{self.counter}"
        self.runs[(profile, rid)] = {
            "run_id": rid,
            "status": "queued",
            "idem": idempotency_key,
            "session_id": body.get("session_id"),
            "instructions": body.get("instructions"),
        }
        return {"run_id": rid, "status": "queued"}

    async def get_run(self, profile, run_id):
        self._guard()
        return self.runs[(profile, run_id)]

    async def stream_run_events(self, profile, run_id) -> AsyncIterator[dict[str, Any]]:
        yield {"event": "run.started", "run_id": run_id}
        yield {"event": "message.delta", "delta": "hola"}
        yield {"event": "tool.started", "tool_name": "calculator"}
        yield {"event": "subagent.start", "task_id": "child"}
        yield {
            "event": "reasoning.available",
            "text": "Resumen seguro",
            "reasoning_content": "hidden",
        }
        yield {"event": "assistant.completed", "content": "hola"}
        self.runs[(profile, run_id)]["status"] = "completed"
        yield {"event": "run.completed", "run_id": run_id}

    async def cancel_run(self, profile, run_id):
        self.runs[(profile, run_id)]["status"] = "cancelled"
        return self.runs[(profile, run_id)]

    async def steer_run(self, profile, run_id, instruction):
        return {"accepted": True}

    async def answer_approval(self, profile, run_id, request_id, choice):
        return {"resolved": 1}

    async def list_scheduled_tasks(self, profile, *, include_disabled=True):
        rows = list(self.jobs[profile].values())
        if not include_disabled:
            rows = [row for row in rows if row.get("enabled", True)]
        return {"jobs": rows}

    async def get_scheduled_task(self, profile, job_id):
        from hermes_mobile.api.errors import MobileError

        if job_id not in self.jobs[profile]:
            raise MobileError("not_found", "missing", 404)
        return {"job": self.jobs[profile][job_id]}

    async def update_scheduled_task(self, profile, job_id, body):
        self.jobs[profile][job_id].update(body)
        return {"job": self.jobs[profile][job_id]}

    async def delete_scheduled_task(self, profile, job_id):
        self.jobs[profile].pop(job_id, None)

    async def pause_scheduled_task(self, profile, job_id):
        self.jobs[profile][job_id].update(enabled=False, state="paused")
        return {"job": self.jobs[profile][job_id]}

    async def resume_scheduled_task(self, profile, job_id):
        self.jobs[profile][job_id].update(enabled=True, state="scheduled")
        return {"job": self.jobs[profile][job_id]}

    async def run_scheduled_task(self, profile, job_id):
        self.jobs[profile][job_id]["next_run_at"] = "now"
        return {"job": self.jobs[profile][job_id]}

    def list_executions(
        self, _profile_home, job_id, *, limit=50, before_claimed_at=None
    ):
        profile = "default" if ("default", job_id) in self.executions else "mujer"
        rows = self.executions[(profile, job_id)]
        if before_claimed_at:
            rows = [row for row in rows if row["claimed_at"] < before_claimed_at]
        return rows[:limit]

    def get_execution(self, _profile_home, execution_id):
        for rows in self.executions.values():
            for row in rows:
                if row["id"] == execution_id:
                    return row
        return None

    def update_job_metadata(self, _profile_home, job_id, updates):
        for jobs in self.jobs.values():
            if job_id in jobs:
                jobs[job_id].update(updates)
                return jobs[job_id]
        return None

    def execution_output(self, _profile_home, job_id, execution):
        return self.outputs.get((job_id, execution["id"]))

    def append_conversation_result(self, _profile_home, session_id, text):
        self.mirrored_results.append((session_id, text))

    async def models(self, profile):
        self._guard()
        return {
            "data": [
                {
                    "id": "mock-model-next",
                    "reasoning": {
                        "supported": True,
                        "can_disable": False,
                        "efforts": [
                            "minimal",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                            "max",
                            "ultra",
                        ],
                    },
                },
                {
                    "id": "mock-model",
                    "reasoning": {
                        "supported": True,
                        "can_disable": True,
                        "efforts": [
                            "none",
                            "minimal",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                            "max",
                            "ultra",
                        ],
                    },
                },
                {
                    "id": "mock-no-reasoning",
                    "reasoning": {
                        "supported": False,
                        "can_disable": None,
                        "efforts": [],
                    },
                },
            ],
            "default": "mock-model",
            "provider": "mock",
        }

    def read(self, _profile_home, *, model=""):
        return dict(self.profile_preferences_data)

    def update(
        self,
        _profile_home,
        *,
        model,
        update_model,
        reasoning_effort,
        update_reasoning,
    ):
        call = {
            "model": model,
            "update_model": update_model,
            "reasoning_effort": reasoning_effort,
            "update_reasoning": update_reasoning,
        }
        self.preference_updates.append(call)
        if update_model:
            self.profile_preferences_data["model"] = model
        if update_reasoning:
            self.profile_preferences_data["reasoning_effort"] = reasoning_effort
        return dict(self.profile_preferences_data)

    async def toolsets(self, profile):
        self._guard()
        return {
            "data": [
                {"name": "hermes_mobile", "label": "Hermes Mobile", "enabled": True}
            ]
        }


@pytest.fixture
def fake_facade():
    return FakeFacade()


@pytest.fixture
async def runtime(tmp_path: Path, fake_facade: FakeFacade):
    (tmp_path / "profiles" / "mujer").mkdir(parents=True)
    config = MobileConfig(
        default_home=tmp_path, push=PushConfig(enabled=False), pairing_ttl_seconds=600
    )
    value = MobileRuntime(
        config,
        facade=fake_facade,
        cron_reader=fake_facade,
        profile_preferences=fake_facade,
    )  # type: ignore[arg-type]
    yield value
    if value.started:
        await value.close()


@pytest.fixture
async def client(runtime: MobileRuntime):
    app = web.Application()
    MobileAPI(runtime).wire(app)
    async with TestClient(TestServer(app)) as value:
        yield value


async def pair_client(
    client: TestClient,
    runtime: MobileRuntime,
    profile: str = "default",
    installation_id: str = "installation-0001",
) -> dict[str, Any]:
    pairing = await asyncio.to_thread(
        runtime.control.create_pairing, profile, profile.title(), 600
    )
    response = await client.post(
        f"/p/{profile}/v1/mobile/auth/pair",
        json={
            "pairing_token": pairing["token"],
            "device": {
                "installation_id": installation_id,
                "name": "Test phone",
                "platform": "ios",
            },
        },
    )
    assert response.status == 201, await response.text()
    return await response.json()


@pytest.fixture
async def auth(client: TestClient, runtime: MobileRuntime):
    paired = await pair_client(client, runtime)
    return paired, {"Authorization": f"Bearer {paired['access_token']}"}

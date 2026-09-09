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

    async def set_conversation_model(self, profile, session_id, model):
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

    async def models(self, profile):
        self._guard()
        return {"data": [{"id": "mock-model"}]}

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
    value = MobileRuntime(config, facade=fake_facade)  # type: ignore[arg-type]
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

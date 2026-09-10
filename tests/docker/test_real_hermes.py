from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.docker
BASE = os.environ.get("HERMES_MOBILE_TEST_URL", "http://127.0.0.1:18642")
DATA = Path(os.environ.get("HERMES_MOBILE_TEST_DATA", "/tmp/hermes-mobile-test-data"))
EXPO = os.environ.get("HERMES_MOBILE_EXPO_URL", "http://127.0.0.1:8082")


def _fixture(name: str) -> dict:
    text = (DATA / name).read_text(encoding="utf-8")
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


async def _pair(client: httpx.AsyncClient, profile: str) -> dict:
    seed = _fixture(f"test-pair-{profile}.json")
    response = await client.post(
        f"/p/{profile}/v1/mobile/auth/pair",
        json={
            "pairing_token": seed["pairing_token"],
            "device": {
                "installation_id": f"docker-{profile}-{uuid.uuid4()}",
                "name": f"Docker {profile}",
                "platform": "ios",
                "app_version": "1.0.0",
                "locale": "es-ES",
                "timezone": "Europe/Madrid",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_conversation(
    client: httpx.AsyncClient, profile: str, token: str, title: str
) -> dict:
    response = await client.post(
        f"/p/{profile}/v1/mobile/conversations",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={"title": title, "model": None},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _wait_run(
    client: httpx.AsyncClient, profile: str, token: str, run_id: str
) -> dict:
    for _ in range(120):
        response = await client.get(
            f"/p/{profile}/v1/mobile/runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0.25)
    pytest.fail("real Hermes run did not terminate")


@pytest.mark.asyncio
async def test_real_multiplexed_hermes_mobile_surface():
    async with httpx.AsyncClient(base_url=BASE, timeout=40) as client:
        portal = await client.get("/")
        assert portal.status_code == 200
        assert "Hermes Mobile" in portal.text
        assert "frame-ancestors 'none'" in portal.headers["Content-Security-Policy"]
        assert portal.headers["Cache-Control"] == "no-store"
        portal_status = await client.get("/v1/mobile/admin/status")
        assert portal_status.json() == {
            "configured": False,
            "authenticated": False,
        }
        bootstrap = _fixture("test-admin-bootstrap.json")
        assert bootstrap["setup_url"].startswith("https://hermes.test/#setup=")
        registration = await client.post(
            "/v1/mobile/admin/register/options",
            headers={"Origin": "https://hermes.test"},
            json={"bootstrap_token": bootstrap["bootstrap_token"]},
        )
        assert registration.status_code == 200, registration.text
        registration_options = registration.json()["publicKey"]
        assert registration_options["rp"]["id"] == "hermes.test"
        assert (
            registration_options["authenticatorSelection"]["residentKey"] == "required"
        )

        assert (await client.get("/p/default/v1/mobile/health")).json()[
            "status"
        ] == "ok"
        default, mujer = await _pair(client, "default"), await _pair(client, "mujer")
        default_token, mujer_token = default["access_token"], mujer["access_token"]
        default_headers = {"Authorization": f"Bearer {default_token}"}
        mujer_headers = {"Authorization": f"Bearer {mujer_token}"}

        assert _fixture("test-doctor-default.json")["status"] == "ok"
        assert _fixture("test-doctor-mujer.json")["status"] == "ok"
        capabilities = (
            await client.get(
                "/p/default/v1/mobile/capabilities", headers=default_headers
            )
        ).json()
        assert (
            capabilities["streaming"]
            and capabilities["attachments"]
            and capabilities["sync"]
        )

        device = await client.post(
            "/p/default/v1/mobile/devices",
            headers=default_headers,
            json={
                "installation_id": default["device_id"].replace(
                    "dev_", "docker-install-"
                )[:120],
                "name": "Docker default",
                "platform": "ios",
                "push_provider": "expo",
                "push_token": "ExponentPushToken[docker-real-device]",
            },
        )
        # installation_id is immutable and intentionally rejected when it differs.
        assert device.status_code == 403
        me = (
            await client.get("/p/default/v1/mobile/me", headers=default_headers)
        ).json()
        device = await client.post(
            "/p/default/v1/mobile/devices",
            headers=default_headers,
            json={
                "installation_id": me["device"]["installation_id"],
                "name": "Docker default",
                "platform": "ios",
                "push_provider": "expo",
                "push_token": "ExponentPushToken[docker-real-device]",
            },
        )
        assert device.status_code == 200, device.text

        default_conv = await _create_conversation(
            client, "default", default_token, "Default private"
        )
        mujer_conv = await _create_conversation(
            client, "mujer", mujer_token, "Mujer private"
        )
        mismatch = await client.get("/p/mujer/v1/mobile/me", headers=default_headers)
        assert (
            mismatch.status_code == 403
            and mismatch.json()["error"]["code"] == "profile_mismatch"
        )
        cross = await client.get(
            f"/p/mujer/v1/mobile/conversations/{default_conv['id']}",
            headers=mujer_headers,
        )
        assert cross.status_code == 404
        default_list = (
            await client.get(
                "/p/default/v1/mobile/conversations", headers=default_headers
            )
        ).json()
        mujer_list = (
            await client.get("/p/mujer/v1/mobile/conversations", headers=mujer_headers)
        ).json()
        assert default_conv["id"] in {item["id"] for item in default_list["items"]}
        assert mujer_conv["id"] in {item["id"] for item in mujer_list["items"]}

        mujer_run_response = await client.post(
            f"/p/mujer/v1/mobile/conversations/{mujer_conv['id']}/runs",
            headers={**mujer_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "client_message_id": str(uuid.uuid4()),
                "input": [
                    {
                        "type": "text",
                        "text": "Reply from the real secondary Hermes profile.",
                    }
                ],
            },
        )
        assert mujer_run_response.status_code == 202, mujer_run_response.text
        mujer_terminal = await _wait_run(
            client, "mujer", mujer_token, mujer_run_response.json()["run_id"]
        )
        assert mujer_terminal["status"] == "completed"

        upload = await client.post(
            "/p/default/v1/mobile/attachments",
            headers=default_headers,
            files={
                "file": ("facts.txt", b"The verified answer is 42.\n", "text/plain")
            },
            data={
                "client_attachment_id": str(uuid.uuid4()),
                "conversation_id": default_conv["id"],
            },
        )
        assert upload.status_code == 202, upload.text
        attachment = upload.json()
        for _ in range(80):
            attachment = (
                await client.get(
                    f"/p/default/v1/mobile/attachments/{attachment['id']}",
                    headers=default_headers,
                )
            ).json()
            if attachment["status"] == "ready":
                break
            await asyncio.sleep(0.1)
        assert attachment["status"] == "ready"

        run_response = await client.post(
            f"/p/default/v1/mobile/conversations/{default_conv['id']}/runs",
            headers={**default_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "client_message_id": str(uuid.uuid4()),
                "input": [
                    {"type": "text", "text": "Answer from the attached facts."},
                    {"type": "attachment", "attachment_id": attachment["id"]},
                ],
            },
        )
        assert run_response.status_code == 202, run_response.text
        run = run_response.json()
        terminal = await _wait_run(client, "default", default_token, run["run_id"])
        assert terminal["status"] == "completed", terminal

        stream = await client.get(
            f"/p/default/v1/mobile/runs/{run['run_id']}/events", headers=default_headers
        )
        assert stream.status_code == 200
        assert (
            "event: run.started" in stream.text
            and "event: run.completed" in stream.text
        )
        messages = (
            await client.get(
                f"/p/default/v1/mobile/conversations/{default_conv['id']}/messages",
                headers=default_headers,
            )
        ).json()
        assert any(
            "Hermes real respondió" in json.dumps(item, ensure_ascii=False)
            for item in messages["items"]
        )
        sync = (
            await client.get(
                "/p/default/v1/mobile/sync?cursor=sync_0", headers=default_headers
            )
        ).json()
        assert {change["type"] for change in sync["changes"]} >= {
            "conversation.created",
            "attachment.updated",
            "run.updated",
            "message.created",
        }

        for _ in range(40):
            pushes = (await client.get(f"{EXPO}/messages")).json()["messages"]
            if pushes:
                break
            await asyncio.sleep(0.25)
        assert len(pushes) == 1
        assert pushes[0]["data"]["profile"] == "default"
        assert pushes[0]["data"]["conversation_id"] == default_conv["id"]

        # The hub projects Hermes' real job store, execution ledger and output
        # files. An inherited API origin is normalized before the task fires.
        native_job = await client.post(
            "/p/default/api/jobs",
            headers={"Authorization": "Bearer default-api-server-key-0000000000000001"},
            json={
                "name": "Docker scheduled result",
                "schedule": "0 0 * * *",
                "prompt": "Return the scheduled integration result.",
                "deliver": "origin",
            },
        )
        assert native_job.status_code == 200, native_job.text
        scheduled = (
            await client.get(
                "/p/default/v1/mobile/scheduled-tasks", headers=default_headers
            )
        ).json()
        task = next(
            item
            for item in scheduled["items"]
            if item["name"] == "Docker scheduled result"
        )
        assert task["delivery"]["primary"] == "hub"
        normalized = await client.get(
            f"/p/default/api/jobs/{native_job.json()['job']['id']}",
            headers={"Authorization": "Bearer default-api-server-key-0000000000000001"},
        )
        assert normalized.json()["job"]["deliver"] == "local"
        fired = await client.post(
            f"/p/default/v1/mobile/scheduled-tasks/{task['id']}/run",
            headers=default_headers,
        )
        assert fired.status_code == 202, fired.text
        scheduled_runs = []
        # The production ticker intentionally polls at a coarse interval; the
        # run-now endpoint marks the job due rather than bypassing that native
        # claim path.
        for _ in range(360):
            scheduled_runs = (
                await client.get(
                    f"/p/default/v1/mobile/scheduled-tasks/{task['id']}/runs",
                    headers=default_headers,
                )
            ).json()["items"]
            if scheduled_runs and scheduled_runs[0]["status"] in {
                "completed",
                "failed",
                "unknown",
            }:
                break
            await asyncio.sleep(0.25)
        assert scheduled_runs and scheduled_runs[0]["status"] == "completed"
        scheduled_result = await client.get(
            f"/p/default/v1/mobile/scheduled-runs/{scheduled_runs[0]['id']}",
            headers=default_headers,
        )
        assert scheduled_result.status_code == 200, scheduled_result.text
        assert "Hermes real respondió" in scheduled_result.json()["result"]
        assert (
            await client.post(
                f"/p/default/v1/mobile/scheduled-runs/{scheduled_runs[0]['id']}/read",
                headers=default_headers,
            )
        ).status_code == 204
        assert (
            await client.delete(
                f"/p/default/v1/mobile/scheduled-tasks/{task['id']}",
                headers=default_headers,
            )
        ).status_code == 204

        # Exercise a dependency outage in the same authenticated real-world
        # journey. Hermes must complete the run while the push is retained for
        # retry in the durable outbox.
        await client.post(f"{EXPO}/mode/503")
        failed_push_run = await client.post(
            f"/p/default/v1/mobile/conversations/{default_conv['id']}/runs",
            headers={**default_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "client_message_id": str(uuid.uuid4()),
                "input": [
                    {
                        "type": "text",
                        "text": "Complete even if Expo Push is unavailable.",
                    }
                ],
            },
        )
        assert failed_push_run.status_code == 202, failed_push_run.text
        failed_push_terminal = await _wait_run(
            client, "default", default_token, failed_push_run.json()["run_id"]
        )
        assert failed_push_terminal["status"] == "completed"

        db = sqlite3.connect(DATA / "plugin-data" / "hermes-mobile" / "control.db")
        try:
            for _ in range(60):
                row = db.execute(
                    "SELECT status,attempts FROM notification_outbox "
                    "WHERE dedupe_key=?",
                    (failed_push_run.json()["run_id"],),
                ).fetchone()
                if row and row[1] >= 1:
                    break
                await asyncio.sleep(0.1)
            assert row and row[0] in {"pending", "failed"} and row[1] >= 1
        finally:
            db.close()

        after = await client.get("/p/default/v1/mobile/health")
        assert after.status_code == 200 and after.json()["status"] == "ok"
        await client.post(f"{EXPO}/mode/200")

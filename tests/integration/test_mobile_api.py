from __future__ import annotations

import asyncio
import json

from aiohttp import FormData
from conftest import pair_client


async def _conversation(client, headers, title="Trip"):
    response = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": f"create-{title}"},
        json={"title": title, "model": None},
    )
    assert response.status == 201, await response.text()
    return await response.json()


async def test_unified_inbox_can_open_conversation_and_reply(
    client, runtime, fake_facade, auth
):
    _, headers = auth
    store = runtime.store("default")
    item, created = store.create_inbox_item(
        kind="gateway.restarted",
        severity="info",
        title="Gateway reiniciado",
        body="Hermes vuelve a estar disponible.",
        source_type="gateway",
        source_id="boot-test",
        dedupe_key="gateway.restarted:boot-test",
        context={"downtime_seconds": 12, "result": "detalle extenso"},
    )
    assert created is True

    rejected = await client.post(
        f"/p/default/v1/mobile/inbox/{item['public_id']}/reply",
        headers=headers,
        json={
            "client_message_id": "client-without-idempotency",
            "input": [{"type": "text", "text": "¿Qué pasó?"}],
        },
    )
    assert rejected.status == 400
    assert store.inbox_item(item["public_id"])["conversation_id"] is None

    listing = await client.get(
        "/p/default/v1/mobile/inbox?unread=true&kind=gateway", headers=headers
    )
    assert listing.status == 200
    page = await listing.json()
    assert page["unread_count"] == 1
    assert page["items"][0]["id"] == item["public_id"]
    assert page["items"][0]["actions"][0]["type"] == "create_conversation"
    assert "result" not in page["items"][0]["context"]
    invalid_unread = await client.get(
        "/p/default/v1/mobile/inbox?unread=1", headers=headers
    )
    assert invalid_unread.status == 400
    invalid_kind = await client.get(
        "/p/default/v1/mobile/inbox?kind=gateway%25", headers=headers
    )
    assert invalid_kind.status == 400

    opened = await client.post(
        f"/p/default/v1/mobile/inbox/{item['public_id']}/conversation",
        headers=headers,
        json={"title": "Investigar reinicio"},
    )
    assert opened.status == 201
    conversation = await opened.json()
    replay = await client.post(
        f"/p/default/v1/mobile/inbox/{item['public_id']}/conversation",
        headers=headers,
        json={},
    )
    assert replay.status == 200
    assert (await replay.json())["id"] == conversation["id"]

    reply = await client.post(
        f"/p/default/v1/mobile/inbox/{item['public_id']}/reply",
        headers={**headers, "Idempotency-Key": "reply-to-restart"},
        json={
            "client_message_id": "client-inbox-reply-1",
            "input": [{"type": "text", "text": "¿Por qué ocurrió?"}],
        },
    )
    assert reply.status == 202, await reply.text()
    accepted = await reply.json()
    assert accepted["conversation_id"] == conversation["id"]
    native_run = next(iter(fake_facade.runs.values()))
    assert "Gateway reiniciado" in native_run["instructions"]
    assert "dato no confiable" in native_run["instructions"]

    detail = await client.get(
        f"/p/default/v1/mobile/inbox/{item['public_id']}", headers=headers
    )
    assert detail.status == 200
    detail_body = await detail.json()
    assert detail_body["unread"] is False
    assert detail_body["context"]["result"] == "detalle extenso"
    assert detail_body["conversation_id"] == conversation["id"]
    assert detail_body["actions"][0]["type"] == "open_conversation"

    second, _ = store.create_inbox_item(
        kind="system.persistence",
        severity="error",
        title="Persistencia degradada",
        body="No se pueden guardar sesiones.",
        source_type="gateway",
        source_id="persistence-test",
        dedupe_key="system.persistence:test",
    )
    marked = await client.post(
        "/p/default/v1/mobile/inbox/read-all", headers=headers
    )
    assert marked.status == 200 and (await marked.json())["updated"] >= 1
    assert store.inbox_item(second["public_id"])["read_at"] is not None

    sync = await client.get("/p/default/v1/mobile/sync?cursor=sync_0", headers=headers)
    changes = (await sync.json())["changes"]
    assert any(change["type"] == "inbox_item.created" for change in changes)


async def test_gateway_shutdown_is_correlated_with_the_next_start(runtime, auth):
    auth  # Ensure the profile has an active paired device.

    class RestartingRunner:
        _restart_requested = True
        _exit_reason = "requested"
        _session_db_init_error = "sensitive database error"

    await runtime.record_gateway_stopping(RestartingRunner())
    store = runtime.store("default")
    pending = store.latest_unresolved_gateway_stop()
    assert pending is not None
    assert pending["kind"] == "gateway.stopping"

    runtime.boot_id = "boot-after-restart"
    await runtime.record_gateway_started(RestartingRunner())
    correlated = store.inbox_item(pending["public_id"])
    assert correlated["kind"] == "gateway.restarted"
    assert correlated["resolved_at"] is not None
    assert json.loads(correlated["context_json"])["boot_id"] == "boot-after-restart"
    assert store.latest_unresolved_gateway_stop() is None
    persistence = store.inbox_item_by_source(
        "gateway_diagnostic", "boot-after-restart"
    )
    assert persistence is not None
    assert persistence["kind"] == "system.persistence"
    assert "sensitive database error" not in persistence["body"]


async def test_system_auth_devices_refresh_and_logout(client, runtime, auth):
    paired, headers = auth
    health = await client.get("/p/default/v1/mobile/health")
    assert health.status == 200
    assert (await health.json()) == {
        "status": "ok",
        "service": "hermes-mobile",
        "api_version": "1.0",
    }

    capabilities = await client.get(
        "/p/default/v1/mobile/capabilities", headers=headers
    )
    assert capabilities.status == 200
    assert (await capabilities.json())["streaming"] is True
    bootstrap = await client.get("/p/default/v1/mobile/bootstrap", headers=headers)
    assert bootstrap.status == 200 and bootstrap.headers["ETag"]
    bootstrap_body = await bootstrap.json()
    assert bootstrap_body["default_model"] == "mock-model"
    assert bootstrap_body["default_reasoning_effort"] == "medium"
    assert bootstrap_body["preferences"] == {
        "model": "mock-model",
        "reasoning_effort": "medium",
    }
    cached = await client.get(
        "/p/default/v1/mobile/bootstrap",
        headers={**headers, "If-None-Match": bootstrap.headers["ETag"]},
    )
    assert cached.status == 304
    me = await client.get("/p/default/v1/mobile/me", headers=headers)
    assert (await me.json())["device"]["id"] == paired["device_id"]

    update = await client.post(
        "/p/default/v1/mobile/devices",
        headers=headers,
        json={
            "installation_id": "installation-0001",
            "name": "Renamed",
            "platform": "ios",
            "push_provider": "expo",
            "push_token": "ExponentPushToken[abcdefghijk]",
            "notifications": {
                "turn_completed": True,
                "turn_failed": False,
                "approval_required": True,
            },
        },
    )
    assert update.status == 200
    assert (await update.json())["push_registered"] is True
    devices = await client.get("/p/default/v1/mobile/devices", headers=headers)
    body = await devices.json()
    assert body["items"][0]["name"] == "Renamed"
    assert "push_token" not in json.dumps(body)

    refresh = await client.post(
        "/p/default/v1/mobile/auth/refresh",
        json={"refresh_token": paired["refresh_token"]},
    )
    assert refresh.status == 200
    new_session = await refresh.json()
    reuse = await client.post(
        "/p/default/v1/mobile/auth/refresh",
        json={"refresh_token": paired["refresh_token"]},
    )
    assert reuse.status == 401
    new_headers = {"Authorization": f"Bearer {new_session['access_token']}"}
    logout = await client.post("/p/default/v1/mobile/auth/logout", headers=new_headers)
    assert logout.status == 204
    assert (
        await client.get("/p/default/v1/mobile/me", headers=new_headers)
    ).status == 401


async def test_conversation_full_lifecycle_and_idempotency(client, auth):
    _, headers = auth
    conversation = await _conversation(client, headers)
    replay = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": "create-Trip"},
        json={"title": "Trip", "model": None},
    )
    assert replay.status == 201 and (await replay.json())["id"] == conversation["id"]
    conflict = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": "create-Trip"},
        json={"title": "Different", "model": None},
    )
    assert conflict.status == 409

    listing = await client.get(
        "/p/default/v1/mobile/conversations?q=trip", headers=headers
    )
    assert (await listing.json())["items"][0]["id"] == conversation["id"]
    detail = await client.get(
        f"/p/default/v1/mobile/conversations/{conversation['id']}", headers=headers
    )
    assert detail.status == 200
    patched = await client.patch(
        f"/p/default/v1/mobile/conversations/{conversation['id']}",
        headers=headers,
        json={"title": "New title", "pinned": True, "model": "mock-model"},
    )
    assert (await patched.json())["pinned"] is True
    messages = await client.get(
        f"/p/default/v1/mobile/conversations/{conversation['id']}/messages",
        headers=headers,
    )
    assert (await messages.json())["items"] == []
    read = await client.post(
        f"/p/default/v1/mobile/conversations/{conversation['id']}/read",
        headers=headers,
        json={"message_id": "msg_12345678"},
    )
    assert read.status == 204
    forked = await client.post(
        f"/p/default/v1/mobile/conversations/{conversation['id']}/fork",
        headers=headers,
        json={"message_id": "msg_12345678", "title": "Branch"},
    )
    assert forked.status == 201
    deleted = await client.delete(
        f"/p/default/v1/mobile/conversations/{conversation['id']}", headers=headers
    )
    assert deleted.status == 204
    assert (
        await client.get(
            f"/p/default/v1/mobile/conversations/{conversation['id']}", headers=headers
        )
    ).status == 404


async def test_conversation_model_reasoning_and_profile_preference_scope(
    client, fake_facade, auth
):
    _, headers = auth
    created = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": "create-reasoning"},
        json={
            "title": "Reasoning",
            "model": "mock-model-next",
            "reasoning_effort": "high",
        },
    )
    assert created.status == 201, await created.text()
    conversation = await created.json()
    assert conversation["model"] == "mock-model-next"
    assert conversation["reasoning_effort"] == "high"
    assert fake_facade.created_bodies[-1][1] == {
        "title": "Reasoning",
        "model": "mock-model-next",
        "model_options": {"reasoning": {"enabled": True, "effort": "high"}},
        "require_model_lock": True,
    }
    assert fake_facade.preference_updates == [
        {
            "model": "mock-model-next",
            "update_model": True,
            "reasoning_effort": "high",
            "update_reasoning": True,
        }
    ]
    replay = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": "create-reasoning"},
        json={
            "title": "Reasoning",
            "model": "mock-model-next",
            "reasoning_effort": "high",
        },
    )
    assert replay.status == 201
    assert (await replay.json())["id"] == conversation["id"]
    assert len(fake_facade.preference_updates) == 1

    patched = await client.patch(
        f"/p/default/v1/mobile/conversations/{conversation['id']}",
        headers=headers,
        json={"model": "mock-model", "reasoning_effort": "low"},
    )
    assert patched.status == 200, await patched.text()
    assert (await patched.json())["reasoning_effort"] == "low"
    assert fake_facade.model_updates[-1][2:] == ("mock-model", "low")
    assert len(fake_facade.preference_updates) == 1
    assert fake_facade.profile_preferences_data == {
        "model": "mock-model-next",
        "provider": "mock",
        "reasoning_effort": "high",
    }

    inherited = await client.patch(
        f"/p/default/v1/mobile/conversations/{conversation['id']}",
        headers=headers,
        json={"reasoning_effort": None},
    )
    assert inherited.status == 200, await inherited.text()
    assert (await inherited.json())["reasoning_effort"] is None
    assert fake_facade.model_updates[-1][2:] == ("mock-model", None)
    assert len(fake_facade.preference_updates) == 1

    unsupported = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": "create-no-reasoning"},
        json={
            "model": "mock-no-reasoning",
            "reasoning_effort": "high",
        },
    )
    assert unsupported.status == 400
    assert (await unsupported.json())["error"]["code"] == "reasoning_unavailable"

    required = await client.post(
        "/p/default/v1/mobile/conversations",
        headers={**headers, "Idempotency-Key": "create-required-reasoning"},
        json={"model": "mock-model-next", "reasoning_effort": "none"},
    )
    assert required.status == 400
    assert (await required.json())["error"]["code"] == "reasoning_required"


async def test_attachment_run_sse_sync_models_and_toolsets(
    client, runtime, fake_facade, auth
):
    _, headers = auth
    conversation = await _conversation(client, headers, "Attachment run")
    form = FormData()
    form.add_field("client_attachment_id", "local-attachment-1")
    form.add_field("conversation_id", conversation["id"])
    form.add_field(
        "file",
        b"revenue,amount\nQ1,42\n",
        filename="report.csv",
        content_type="text/csv",
    )
    uploaded = await client.post(
        "/p/default/v1/mobile/attachments", headers=headers, data=form
    )
    assert uploaded.status == 202, await uploaded.text()
    attachment = await uploaded.json()
    for _ in range(50):
        response = await client.get(
            f"/p/default/v1/mobile/attachments/{attachment['id']}", headers=headers
        )
        attachment = await response.json()
        if attachment["status"] == "ready":
            break
        await asyncio.sleep(0.02)
    assert attachment["status"] == "ready"
    listing = await client.get(
        f"/p/default/v1/mobile/attachments?conversation_id={conversation['id']}&status=ready",
        headers=headers,
    )
    assert len((await listing.json())["items"]) == 1
    content = await client.get(
        f"/p/default/v1/mobile/attachments/{attachment['id']}/content", headers=headers
    )
    assert content.status == 200 and b"Q1,42" in await content.read()

    started = await client.post(
        f"/p/default/v1/mobile/conversations/{conversation['id']}/runs",
        headers={**headers, "Idempotency-Key": "run-one"},
        json={
            "client_message_id": "client-message-one",
            "input": [
                {"type": "text", "text": "Analyze"},
                {"type": "attachment", "attachment_id": attachment["id"]},
            ],
        },
    )
    assert started.status == 202, await started.text()
    run = await started.json()
    native_run = next(
        item
        for (profile, _run_id), item in fake_facade.runs.items()
        if profile == "default" and item.get("idem") == "run-one"
    )
    assert "deliver='local'" in native_run["instructions"]
    assert "Nunca uses Telegram" in native_run["instructions"]
    replayed = await client.post(
        f"/p/default/v1/mobile/conversations/{conversation['id']}/runs",
        headers={**headers, "Idempotency-Key": "run-one"},
        json={
            "client_message_id": "client-message-one",
            "input": [
                {"type": "text", "text": "Analyze"},
                {"type": "attachment", "attachment_id": attachment["id"]},
            ],
        },
    )
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert (await replayed.json())["run_id"] == run["run_id"]
    conflict = await client.post(
        f"/p/default/v1/mobile/conversations/{conversation['id']}/runs",
        headers={**headers, "Idempotency-Key": "run-one"},
        json={
            "client_message_id": "different",
            "input": [{"type": "text", "text": "Different"}],
        },
    )
    assert conflict.status == 409
    terminal = None
    for _ in range(100):
        status = await client.get(
            f"/p/default/v1/mobile/runs/{run['run_id']}", headers=headers
        )
        terminal = await status.json()
        if terminal["status"] in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)
    assert terminal and terminal["status"] == "completed"
    stream = await client.get(
        f"/p/default/v1/mobile/runs/{run['run_id']}/events", headers=headers
    )
    events = await stream.text()
    assert "event: run.started" in events
    assert "event: tool.started" in events
    assert "event: subagent.started" in events
    assert "event: reasoning.available" in events
    assert "private chain" not in events
    assert "event: run.completed" in events

    models = await client.get("/p/default/v1/mobile/models", headers=headers)
    assert await models.json() == {
        "items": [
            {
                "id": "mock-model-next",
                "name": "mock-model-next",
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
                "name": "mock-model",
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
                "name": "mock-no-reasoning",
                "reasoning": {
                    "supported": False,
                    "can_disable": None,
                    "efforts": [],
                },
            },
        ],
        "default": "mock-model",
        "default_reasoning_effort": "medium",
    }
    toolsets = await client.get("/p/default/v1/mobile/toolsets", headers=headers)
    assert (await toolsets.json())["items"][0]["id"] == "hermes_mobile"
    sync = await client.get("/p/default/v1/mobile/sync?cursor=sync_0", headers=headers)
    changes = (await sync.json())["changes"]
    assert {change["type"] for change in changes} >= {
        "conversation.created",
        "attachment.updated",
        "run.updated",
    }


async def test_profile_isolation_and_uniform_not_found(client, runtime, auth):
    _, default_headers = auth
    mujer = await pair_client(client, runtime, "mujer", "installation-mujer")
    mujer_headers = {"Authorization": f"Bearer {mujer['access_token']}"}
    default_conv = await _conversation(client, default_headers, "Private")
    mismatch = await client.get("/p/mujer/v1/mobile/me", headers=default_headers)
    assert (
        mismatch.status == 403
        and (await mismatch.json())["error"]["code"] == "profile_mismatch"
    )
    cross = await client.get(
        f"/p/mujer/v1/mobile/conversations/{default_conv['id']}", headers=mujer_headers
    )
    assert cross.status == 404


async def test_bad_upload_and_dependency_failure_do_not_break_health(
    client, fake_facade, auth
):
    _, headers = auth
    form = FormData()
    form.add_field("client_attachment_id", "evil-1")
    form.add_field(
        "file", b"MZ" + b"x" * 32, filename="photo.png", content_type="image/png"
    )
    rejected = await client.post(
        "/p/default/v1/mobile/attachments", headers=headers, data=form
    )
    assert rejected.status == 415
    fake_facade.fail = True
    failed = await client.get("/p/default/v1/mobile/capabilities", headers=headers)
    assert failed.status == 500
    text = await failed.text()
    assert "API_SERVER_KEY" not in text and "should-never-leak" not in text
    assert (await client.get("/p/default/v1/mobile/health")).status == 200


async def test_remaining_device_and_validation_surface(client, runtime, auth):
    paired, headers = auth
    missing_auth = await client.get("/p/default/v1/mobile/me")
    assert missing_auth.status == 401 and (await missing_auth.json())["error"][
        "request_id"
    ].startswith("req_")
    invalid_json = await client.post(
        "/p/default/v1/mobile/devices",
        headers={**headers, "Content-Type": "application/json"},
        data="{",
    )
    assert invalid_json.status == 400
    patched = await client.patch(
        f"/p/default/v1/mobile/devices/{paired['device_id']}",
        headers=headers,
        json={"name": "Patched", "locale": "ca-ES"},
    )
    assert patched.status == 200 and (await patched.json())["name"] == "Patched"
    hidden = await client.patch(
        "/p/default/v1/mobile/devices/dev_not_owned",
        headers=headers,
        json={"name": "x"},
    )
    assert hidden.status == 404
    wrong_refresh = await client.post(
        "/p/mujer/v1/mobile/auth/refresh",
        json={"refresh_token": paired["refresh_token"]},
    )
    assert wrong_refresh.status == 403
    bad_cursor = await client.get(
        "/p/default/v1/mobile/conversations?cursor=%%%", headers=headers
    )
    assert bad_cursor.status == 400
    bad_sync = await client.get(
        "/p/default/v1/mobile/sync?cursor=sync_-1", headers=headers
    )
    assert (
        bad_sync.status == 409
        and (await bad_sync.json())["error"]["details"]["reset_cursor"] == "sync_0"
    )

    deleted = await client.delete(
        f"/p/default/v1/mobile/devices/{paired['device_id']}", headers=headers
    )
    assert deleted.status == 204
    assert (await client.get("/p/default/v1/mobile/me", headers=headers)).status == 401


async def test_cancel_steer_approval_retry_and_sse_resume(
    client, runtime, fake_facade, auth
):
    _, headers = auth
    conversation = await _conversation(client, headers, "Control surface")
    store = runtime.store("default")
    native = "manual-running"
    fake_facade.runs[("default", native)] = {"run_id": native, "status": "running"}
    run = store.create_run(conversation["id"], native, "manual-message")
    store.update_run(run["public_id"], "running")
    first = store.append_event(run["public_id"], "run.started", {})

    steered = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/steer",
        headers=headers,
        json={"instruction": "Prioriza la seguridad"},
    )
    assert steered.status == 202
    approval_id = store.ensure_approval(run["public_id"], "native-approval")
    store.update_run(run["public_id"], "waiting_for_approval")
    approved = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/approvals/{approval_id}",
        headers=headers,
        json={"decision": "allow_once"},
    )
    assert approved.status == 200 and (await approved.json())["status"] == "resolved"
    duplicate = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/approvals/{approval_id}",
        headers=headers,
        json={"decision": "deny"},
    )
    assert duplicate.status == 409

    cancelled = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/cancel", headers=headers
    )
    assert cancelled.status == 200 and (await cancelled.json())["status"] == "cancelled"
    retry_missing_key = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/retry", headers=headers
    )
    assert retry_missing_key.status == 400
    retried = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/retry",
        headers={**headers, "Idempotency-Key": "retry-one"},
    )
    assert retried.status == 202
    retry_replay = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/retry",
        headers={**headers, "Idempotency-Key": "retry-one"},
    )
    assert retry_replay.headers["Idempotency-Replayed"] == "true"
    assert (await retry_replay.json())["run_id"] == (await retried.json())["run_id"]
    await asyncio.sleep(0.1)
    retry_body = await retried.json()
    assert (
        await client.get(
            f"/p/default/v1/mobile/runs/{retry_body['run_id']}", headers=headers
        )
    ).status == 200

    resumed = await client.get(
        f"/p/default/v1/mobile/runs/{run['public_id']}/events",
        headers={**headers, "Last-Event-ID": "evt_unknown"},
    )
    text = await resumed.text()
    assert "event: stream.reset" in text and first["event_id"] in text
    finished_steer = await client.post(
        f"/p/default/v1/mobile/runs/{run['public_id']}/steer",
        headers=headers,
        json={"instruction": "too late"},
    )
    assert finished_steer.status == 409


async def test_attachment_retry_delete_and_active_run_guard(
    client, runtime, fake_facade, auth
):
    _, headers = auth
    conversation = await _conversation(client, headers, "Attachment controls")
    store = runtime.store("default")
    original = store.files_root / "originals" / "manual" / "file"
    original.parent.mkdir(parents=True)
    original.write_text("safe content")
    attachment = store.create_attachment(
        {
            "conversation_id": conversation["id"],
            "client_attachment_id": "manual-retry",
            "filename": "manual.txt",
            "safe_filename": "manual.txt",
            "mime_type": "text/plain",
            "size": original.stat().st_size,
            "sha256": "abc",
            "storage_path": str(original),
        }
    )
    store.update_attachment(
        attachment["public_id"], "failed", error_code="extraction_failed"
    )
    retried = await client.post(
        f"/p/default/v1/mobile/attachments/{attachment['public_id']}/retry",
        headers=headers,
    )
    assert retried.status == 202
    for _ in range(50):
        current = await client.get(
            f"/p/default/v1/mobile/attachments/{attachment['public_id']}",
            headers=headers,
        )
        if (await current.json())["status"] == "ready":
            break
        await asyncio.sleep(0.02)
    retry_again = await client.post(
        f"/p/default/v1/mobile/attachments/{attachment['public_id']}/retry",
        headers=headers,
    )
    assert retry_again.status == 409

    native = "attachment-run"
    fake_facade.runs[("default", native)] = {"run_id": native, "status": "running"}
    active = store.create_run(conversation["id"], native, None)
    store.update_run(active["public_id"], "running")
    guarded = await client.delete(
        f"/p/default/v1/mobile/attachments/{attachment['public_id']}", headers=headers
    )
    assert guarded.status == 409
    store.update_run(active["public_id"], "completed")
    deleted = await client.delete(
        f"/p/default/v1/mobile/attachments/{attachment['public_id']}", headers=headers
    )
    assert deleted.status == 204
    assert (
        await client.get(
            f"/p/default/v1/mobile/attachments/{attachment['public_id']}",
            headers=headers,
        )
    ).status == 404


async def test_messages_mapping_filters_and_model_error(client, fake_facade, auth):
    _, headers = auth
    first = await _conversation(client, headers, "Alpha")
    await _conversation(client, headers, "Beta")
    fake_facade.messages[
        (
            "default",
            fake_facade.sessions["default"][
                next(
                    key
                    for key, value in fake_facade.sessions["default"].items()
                    if value["title"] == "Alpha"
                )
            ]["id"],
        )
    ] = [
        {
            "id": "native-message",
            "role": "assistant",
            "content": "Answer",
            "timestamp": 1_788_948_001.0,
            "token_count": 4,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "calculator", "arguments": {"x": 1}},
                }
            ],
        },
        {
            "id": "native-cron-mirror",
            "role": "user",
            "content": "[Cron delivery: Daily report]\nScheduled answer",
            "timestamp": 1_788_948_002.0,
            "token_count": 2,
        },
    ]
    messages = await client.get(
        f"/p/default/v1/mobile/conversations/{first['id']}/messages", headers=headers
    )
    body = await messages.json()
    assert body["items"][0]["content"][0]["text"] == "Answer"
    assert body["items"][0]["content"][1]["type"] == "tool_call"
    assert body["items"][1]["role"] == "assistant"
    assert body["items"][1]["content"][0]["text"] == "Scheduled answer"
    invalid_model = await client.patch(
        f"/p/default/v1/mobile/conversations/{first['id']}",
        headers=headers,
        json={"model": "missing-model"},
    )
    assert invalid_model.status == 400
    page = await client.get(
        "/p/default/v1/mobile/conversations?limit=1", headers=headers
    )
    page_body = await page.json()
    assert page_body["has_more"] is True and page_body["next_cursor"]


async def test_scheduled_task_hub_uses_native_cron_and_optional_conversation(
    client, runtime, fake_facade, auth, monkeypatch
):
    _, headers = auth
    conversation = await _conversation(client, headers, "Scheduled origin")
    mapping = runtime.store("default").conversation(conversation["id"])
    session_id = mapping["hermes_session_id"]
    job_id = "abc123def456"
    fake_facade.jobs["default"][job_id] = {
        "id": job_id,
        "name": "Informe diario",
        "prompt": "Resume las novedades",
        "schedule": {"kind": "cron", "expr": "0 8 * * *"},
        "schedule_display": "0 8 * * *",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-09-11T08:00:00+02:00",
        "last_run_at": "2026-09-10T08:00:02+02:00",
        "last_status": "ok",
        "deliver": "origin",
        "attach_to_session": True,
        "origin": {"platform": "api_server", "chat_id": session_id},
        "created_at": "2026-09-09T12:00:00+02:00",
    }
    old_execution = {
        "id": "exec-old",
        "job_id": job_id,
        "source": "scheduler",
        "status": "completed",
        "claimed_at": "2026-09-10T08:00:00+02:00",
        "started_at": "2026-09-10T08:00:00+02:00",
        "finished_at": "2026-09-10T08:00:02+02:00",
        "delivery_outcome": "local",
        "error": None,
    }
    fake_facade.executions[("default", job_id)] = [old_execution]
    fake_facade.outputs[(job_id, "exec-old")] = "Resultado anterior"

    listing = await client.get("/p/default/v1/mobile/scheduled-tasks", headers=headers)
    assert listing.status == 200, await listing.text()
    task = (await listing.json())["items"][0]
    assert task["delivery"] == {
        "primary": "hub",
        "conversation": {
            "mode": "agent",
            "effective": "origin",
            "conversation_id": conversation["id"],
        },
        "external": None,
    }
    assert fake_facade.jobs["default"][job_id]["deliver"] == "local"
    assert task["unread_count"] == 1
    assert fake_facade.mirrored_results == []

    fresh_execution = {
        **old_execution,
        "id": "exec-fresh",
        "claimed_at": "2026-09-11T08:00:00+02:00",
        "started_at": "2026-09-11T08:00:00+02:00",
        "finished_at": "2026-09-11T08:00:03+02:00",
    }
    fake_facade.executions[("default", job_id)].insert(0, fresh_execution)
    fake_facade.outputs[(job_id, "exec-fresh")] = "Resultado nuevo"
    queued_pushes = []
    original_enqueue_push = runtime.control.enqueue_push

    def capture_push(profile, kind, dedupe_key, payload):
        queued_pushes.append((profile, kind, dedupe_key, payload))
        return original_enqueue_push(profile, kind, dedupe_key, payload)

    monkeypatch.setattr(runtime.control, "enqueue_push", capture_push)
    await runtime.reconcile_scheduled_tasks("default")
    assert fake_facade.mirrored_results == [
        (session_id, "[Cron delivery: Informe diario]\nResultado nuevo")
    ]
    await runtime.reconcile_scheduled_tasks("default")
    assert len(fake_facade.mirrored_results) == 1

    runs = await client.get(
        f"/p/default/v1/mobile/scheduled-tasks/{task['id']}/runs", headers=headers
    )
    run_items = (await runs.json())["items"]
    assert [item["status"] for item in run_items] == ["completed", "completed"]
    assert len(queued_pushes) == 1
    push_profile, push_kind, _dedupe_key, push_payload = queued_pushes[0]
    assert push_profile == "default"
    assert push_kind == "scheduled_task.completed"
    assert push_payload["data"] == {
        "type": "scheduled_task.completed",
        "profile": "default",
        "scheduled_task_id": task["id"],
        "scheduled_run_id": run_items[0]["id"],
        "inbox_item_id": push_payload["data"]["inbox_item_id"],
    }
    detail = await client.get(
        f"/p/default/v1/mobile/scheduled-runs/{run_items[0]['id']}", headers=headers
    )
    assert (await detail.json())["result"] == "Resultado nuevo"
    marked = await client.post(
        f"/p/default/v1/mobile/scheduled-runs/{run_items[0]['id']}/read",
        headers=headers,
    )
    assert marked.status == 204
    inbox_detail = await client.get(
        f"/p/default/v1/mobile/inbox/{push_payload['data']['inbox_item_id']}",
        headers=headers,
    )
    assert inbox_detail.status == 200
    assert (await inbox_detail.json())["unread"] is False

    patched = await client.patch(
        f"/p/default/v1/mobile/scheduled-tasks/{task['id']}",
        headers=headers,
        json={"conversation_delivery": "hub_only", "name": "Informe"},
    )
    patched_body = await patched.json()
    assert patched_body["name"] == "Informe"
    assert patched_body["delivery"]["conversation"] == {
        "mode": "hub_only",
        "effective": "hub_only",
        "conversation_id": conversation["id"],
    }
    assert fake_facade.jobs["default"][job_id]["attach_to_session"] is False

    paused = await client.post(
        f"/p/default/v1/mobile/scheduled-tasks/{task['id']}/pause", headers=headers
    )
    assert (await paused.json())["state"] == "paused"
    resumed = await client.post(
        f"/p/default/v1/mobile/scheduled-tasks/{task['id']}/resume", headers=headers
    )
    assert (await resumed.json())["state"] == "scheduled"
    triggered = await client.post(
        f"/p/default/v1/mobile/scheduled-tasks/{task['id']}/run", headers=headers
    )
    assert triggered.status == 202
    deleted = await client.delete(
        f"/p/default/v1/mobile/scheduled-tasks/{task['id']}", headers=headers
    )
    assert deleted.status == 204
    store = runtime.store("default")
    assert store.scheduled_task(task["id"]) is None
    assert all(store.scheduled_run(item["id"]) is None for item in run_items)
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM scheduled_run_state WHERE task_id=?", (task["id"],)
        ).fetchone()[0] == 0
        deletion = connection.execute(
            "SELECT operation,payload_json FROM sync_journal "
            "WHERE entity_type='scheduled_task' AND entity_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
    assert deletion["operation"] == "deleted"
    assert json.loads(deletion["payload_json"])["deleted_at"]

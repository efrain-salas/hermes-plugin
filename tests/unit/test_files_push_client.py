from __future__ import annotations

import json
import sys
import types
import zipfile

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hermes_mobile.api.errors import MobileError
from hermes_mobile.config import MobileConfig, PushConfig
from hermes_mobile.constants import REASONING_EFFORTS
from hermes_mobile.files.extraction import ExtractionError, extract_attachment
from hermes_mobile.files.tool import read_attachment
from hermes_mobile.hermes.api_client import HermesAPIClient
from hermes_mobile.notifications.expo import (
    ExpoPermanentError,
    ExpoTemporaryError,
    check_expo_receipt,
    send_expo,
)
from hermes_mobile.notifications.worker import PushWorker
from hermes_mobile.persistence.repositories import ControlStore, ProfileStore
from hermes_mobile.runtime import MobileRuntime
from hermes_mobile.security.tokens import SecretBox


def test_extract_text_docx_image_and_reject_binary(tmp_path):
    text = tmp_path / "note.txt"
    text.write_text("hola")
    output = tmp_path / "out" / "note.md"
    assert extract_attachment(text, output, "text/plain") == str(output)
    assert "no confiable" in output.read_text() and output.read_text().endswith("hola")

    docx = tmp_path / "note.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", "<w:p><w:t>Uno &amp; dos</w:t></w:p>")
    docx_out = tmp_path / "docx.md"
    extract_attachment(
        docx,
        docx_out,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "Uno & dos" in docx_out.read_text()
    assert extract_attachment(text, tmp_path / "image.md", "image/png") is None
    text.write_bytes(b"bad\x00binary")
    with pytest.raises(ExtractionError, match="binary_content"):
        extract_attachment(text, output, "text/plain")
    invalid_docx = tmp_path / "invalid.docx"
    with zipfile.ZipFile(invalid_docx, "w") as archive:
        archive.writestr("other.xml", "x")
    with pytest.raises(ExtractionError, match="invalid_docx"):
        extract_attachment(
            invalid_docx,
            output,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    pypdf = pytest.importorskip("pypdf")
    pdf = tmp_path / "blank.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf.open("wb") as handle:
        writer.write(handle)
    pdf_out = tmp_path / "pdf.md"
    assert extract_attachment(pdf, pdf_out, "application/pdf") == str(pdf_out)


def test_attachment_tool_is_profile_scoped_and_bounded(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path)
    store.initialize()
    conversation = store.ensure_conversation("native-1")
    original = store.files_root / "originals" / "a" / "file"
    original.parent.mkdir(parents=True)
    original.write_text("raw")
    extracted = store.files_root / "extracted" / "a" / "content.md"
    extracted.parent.mkdir(parents=True)
    extracted.write_text("0123456789")
    attachment = store.create_attachment(
        {
            "conversation_id": conversation["public_id"],
            "client_attachment_id": "client-a",
            "filename": "a.txt",
            "safe_filename": "a.txt",
            "mime_type": "text/plain",
            "size": 3,
            "sha256": "abc",
            "storage_path": str(original),
        }
    )
    store.update_attachment(
        attachment["public_id"], "ready", extracted_path=str(extracted)
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(get_hermes_home=lambda: tmp_path),
    )
    result = json.loads(
        read_attachment(
            {
                "attachment_id": attachment["public_id"],
                "conversation_id": conversation["public_id"],
                "offset": 2,
                "max_chars": 4,
            }
        )
    )
    assert result["text"] == "2345" and result["has_more"] is True
    denied = json.loads(
        read_attachment(
            {"attachment_id": attachment["public_id"], "conversation_id": "conv_other"}
        )
    )
    assert denied == {"ok": False, "error": "attachment_not_found"}


@pytest.mark.asyncio
async def test_expo_protocol_success_temporary_and_permanent():
    state = {"status": 200, "body": {"data": {"status": "ok", "id": "ticket-1"}}}

    async def handler(_request):
        return web.json_response(state["body"], status=state["status"])

    app = web.Application()
    app.router.add_post("/push", handler)
    app.router.add_post("/getReceipts", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        endpoint = str(server.make_url("/push"))
        assert (
            await send_expo(session, endpoint, "ExpoPushToken[x]", {"title": "x"}, 2)
            == "ticket-1"
        )
        state["status"] = 503
        with pytest.raises(ExpoTemporaryError, match="http_503"):
            await send_expo(session, endpoint, "token", {}, 2)
        state.update(status=400, body={})
        with pytest.raises(ExpoPermanentError, match="http_400"):
            await send_expo(session, endpoint, "token", {}, 2)
        state.update(
            status=200,
            body={
                "data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}
            },
        )
        with pytest.raises(ExpoPermanentError, match="DeviceNotRegistered"):
            await send_expo(session, endpoint, "token", {}, 2)
        state["body"] = {"unexpected": True}
        with pytest.raises(ExpoTemporaryError, match="invalid_response"):
            await send_expo(session, endpoint, "token", {}, 2)
        state["body"] = {"data": {"ticket-1": {"status": "ok"}}}
        await check_expo_receipt(session, endpoint, "ticket-1", 2)
        state["body"] = {"data": {}}
        with pytest.raises(ExpoTemporaryError, match="receipt_pending"):
            await check_expo_receipt(session, endpoint, "ticket-1", 2)
        state.update(status=503, body={})
        with pytest.raises(ExpoTemporaryError, match="receipt_http_503"):
            await check_expo_receipt(session, endpoint, "ticket-1", 2)
        state.update(status=400, body={})
        with pytest.raises(ExpoPermanentError, match="receipt_http_400"):
            await check_expo_receipt(session, endpoint, "ticket-1", 2)
        state.update(
            status=200,
            body={
                "data": {
                    "ticket-1": {
                        "status": "error",
                        "details": {"error": "DeviceNotRegistered"},
                    }
                }
            },
        )
        with pytest.raises(ExpoPermanentError, match="DeviceNotRegistered"):
            await check_expo_receipt(session, endpoint, "ticket-1", 2)


@pytest.mark.asyncio
async def test_push_worker_delivery_retry_and_device_revocation(tmp_path, monkeypatch):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    pair = store.create_pairing("default", "Alice", 600)
    paired = store.consume_pairing(
        "default",
        pair["token"],
        {"installation_id": "push-install", "name": "Phone", "platform": "ios"},
        ("devices:self",),
    )
    box = SecretBox(tmp_path / "data.key")
    device_id = paired["device"]["id"]
    store.update_device(
        device_id, {"push_token_encrypted": box.encrypt("ExpoPushToken[test]")}
    )
    store.enqueue_push("default", "run.completed", "run-1", {"title": "Done"})
    worker = PushWorker(
        store, box, PushConfig(enabled=True, endpoint="http://unused", max_attempts=2)
    )
    worker.session = aiohttp.ClientSession()

    async def delivered(*_args, **_kwargs):
        return "ticket-ok"

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_expo", delivered)
    await worker._send(store.pending_push()[0])
    with store.connect() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM notification_outbox WHERE dedupe_key='run-1'"
            ).fetchone()
        )
    assert (
        row["status"] == "receipt_pending" and row["provider_ticket_id"] == "ticket-ok"
    )

    async def receipt_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "hermes_mobile.notifications.worker.check_expo_receipt", receipt_ok
    )
    await worker._receipt(
        {**row, "push_token_encrypted": box.encrypt("ExpoPushToken[test]")}
    )
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM notification_outbox WHERE dedupe_key='run-1'"
            ).fetchone()[0]
            == "delivered"
        )

    store.enqueue_push("default", "run.failed", "run-2", {"title": "Failed"})

    async def gone(*_args, **_kwargs):
        raise ExpoPermanentError("DeviceNotRegistered")

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_expo", gone)
    await worker._send(store.pending_push()[0])
    assert store.list_devices(paired["user"]["id"])[0]["push_token_encrypted"] is None
    await worker.close()


@pytest.mark.asyncio
async def test_push_worker_temporary_retry_and_attempt_limit(tmp_path, monkeypatch):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    pair = store.create_pairing("default", "Alice", 600)
    paired = store.consume_pairing(
        "default",
        pair["token"],
        {"installation_id": "push-install", "name": "Phone", "platform": "ios"},
        ("devices:self",),
    )
    box = SecretBox(tmp_path / "data.key")
    store.update_device(
        paired["device"]["id"], {"push_token_encrypted": box.encrypt("token")}
    )
    store.enqueue_push("default", "run.completed", "retry", {"title": "Done"})
    worker = PushWorker(
        store, box, PushConfig(enabled=True, endpoint="http://unused", max_attempts=2)
    )
    worker.session = aiohttp.ClientSession()

    async def temporary(*_args, **_kwargs):
        raise ExpoTemporaryError("offline")

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_expo", temporary)
    row = store.pending_push()[0]
    await worker._send(row)
    with store.connect() as conn:
        pending = dict(conn.execute("SELECT * FROM notification_outbox").fetchone())
    assert pending["status"] == "pending" and pending["attempts"] == 1
    await worker._send({**pending, "push_token_encrypted": box.encrypt("token")})
    with store.connect() as conn:
        failed = conn.execute("SELECT * FROM notification_outbox").fetchone()
    assert failed["status"] == "failed" and failed["attempts"] == 2
    await worker.close()


@pytest.mark.asyncio
async def test_loopback_client_auth_paths_errors_and_sse():
    seen: list[tuple[str, str]] = []
    model_updates: list[dict] = []

    async def handler(request):
        seen.append((request.method, request.path))
        assert request.headers["Authorization"] == "Bearer scoped-secret"
        if request.path.endswith("/api/model/options"):
            return web.json_response(
                {
                    "model": "gpt-current",
                    "provider": "openai-codex",
                    "providers": [
                        {
                            "slug": "other",
                            "is_current": False,
                            "models": ["other-model"],
                        },
                        {
                            "slug": "openai-codex",
                            "is_current": True,
                            "models": ["gpt-next", "gpt-current", "gpt-next"],
                            "capabilities": {
                                "gpt-next": {
                                    "reasoning": True,
                                    "can_disable_reasoning": False,
                                },
                                "gpt-current": {"reasoning": False},
                            },
                        },
                    ],
                }
            )
        if request.path.endswith("/api/sessions/s1/model"):
            model_updates.append(await request.json())
            return web.json_response({"session": {"id": "s1"}})
        if request.path.endswith("/events"):
            return web.Response(
                text='data: {"event":"run.started"}\n\ndata: {"event":"run.completed"}\n\n',
                content_type="text/event-stream",
            )
        if request.path.endswith("/bad"):
            return web.json_response({"error": {"code": "bad"}}, status=409)
        if request.method == "DELETE":
            return web.Response(status=204)
        if request.path.endswith("/fork"):
            return web.json_response({"session": {"id": "s2"}}, status=201)
        if request.path.endswith("/api/sessions") and request.method == "POST":
            return web.json_response({"session": {"id": "s1"}}, status=201)
        if request.path.endswith("/v1/runs"):
            return web.json_response({"run_id": "r1"}, status=202)
        return web.json_response({"data": [{"id": "mock", "name": "tools"}]})

    app = web.Application()
    app.router.add_route("*", "/p/{profile}/{tail:.*}", handler)
    async with TestServer(app) as server:
        client = HermesAPIClient(
            str(server.make_url("/")), key_provider=lambda _profile: "scoped-secret"
        )
        assert (await client.create_conversation("mujer", {"title": "x"}))["session"][
            "id"
        ] == "s1"
        await client.list_conversations("mujer", limit=3)
        await client.capabilities("mujer")
        await client.get_conversation("mujer", "s1")
        await client.update_conversation("mujer", "s1", {"title": "new"})
        await client.set_conversation_model("mujer", "s1", "mock", "high")
        assert model_updates == [
            {
                "model": "mock",
                "model_options": {"reasoning": {"enabled": True, "effort": "high"}},
            }
        ]
        await client.get_messages("mujer", "s1", limit=2)
        await client.fork_conversation("mujer", "s1", {"title": "fork"})
        assert await client.models("mujer") == {
            "data": [
                {
                    "id": "gpt-next",
                    "object": "model",
                    "owned_by": "openai-codex",
                    "reasoning": {
                        "supported": True,
                        "can_disable": False,
                        "efforts": list(REASONING_EFFORTS[1:]),
                    },
                },
                {
                    "id": "gpt-current",
                    "object": "model",
                    "owned_by": "openai-codex",
                    "reasoning": {
                        "supported": False,
                        "can_disable": None,
                        "efforts": [],
                    },
                },
            ],
            "default": "gpt-current",
            "provider": "openai-codex",
        }
        await client.toolsets("mujer")
        assert (await client.create_run("mujer", {"input": "x"}, "idem"))[
            "run_id"
        ] == "r1"
        await client.get_run("mujer", "r1")
        await client.cancel_run("mujer", "r1")
        await client.steer_run("mujer", "r1", "focus")
        await client.answer_approval("mujer", "r1", "approval", "once")
        events = [event async for event in client.stream_run_events("mujer", "r1")]
        assert [event["event"] for event in events] == ["run.started", "run.completed"]
        await client.delete_conversation("mujer", "s1")
        with pytest.raises(MobileError) as error:
            await client._request("GET", "mujer", "/bad", expected={200})
        assert error.value.code == "bad" and error.value.status == 409
        await client.close()
    assert all(path.startswith("/p/mujer/") for _, path in seen)


@pytest.mark.asyncio
async def test_loopback_network_error_is_typed():
    client = HermesAPIClient("http://127.0.0.1:1", key_provider=lambda _profile: "key")
    with pytest.raises(MobileError) as error:
        await client.capabilities("default")
    assert error.value.code == "gateway_unavailable" and error.value.retryable is True
    await client.close()


@pytest.mark.asyncio
async def test_runtime_synthesizes_start_approval_and_reconciles(tmp_path, monkeypatch):
    class Facade:
        async def start(self):
            pass

        async def close(self):
            pass

        async def stream_run_events(self, _profile, _run):
            yield {"event": "approval.request", "request_id": "native-approval"}
            yield {"event": "approval.responded", "request_id": "native-approval"}
            yield {"event": "run.completed"}

        async def get_run(self, _profile, _run):
            return {"status": "completed"}

    runtime = MobileRuntime(
        MobileConfig(default_home=tmp_path, push=PushConfig(enabled=False)), Facade()
    )
    runtime.control.initialize()
    store = runtime.store("default")
    conversation = store.ensure_conversation("native-session")
    run = store.create_run(conversation["public_id"], "native-run", None)
    await runtime._mirror_run("default", run["public_id"], "native-run")
    events, _ = store.events_after(run["public_id"])
    assert [event["type"] for event in events] == [
        "run.started",
        "approval.requested",
        "approval.resolved",
        "run.completed",
    ]
    assert store.run(run["public_id"])["status"] == "completed"

    second = store.create_run(conversation["public_id"], "native-reconcile", None)
    calls = 0
    original_sleep = __import__("asyncio").sleep

    async def one_iteration(_seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise __import__("asyncio").CancelledError

    monkeypatch.setattr("hermes_mobile.runtime.asyncio.sleep", one_iteration)
    with pytest.raises(__import__("asyncio").CancelledError):
        await runtime._reconcile_runs()
    assert store.run(second["public_id"])["status"] == "completed"
    monkeypatch.setattr("hermes_mobile.runtime.asyncio.sleep", original_sleep)

    missing = await runtime._extract("default", "att_missing")
    assert missing is None
    await runtime.facade.close()

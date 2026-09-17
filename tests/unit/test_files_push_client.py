from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types
import zipfile

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hermes_mobile.api.errors import MobileError
from hermes_mobile.config import (
    APNsCredentials,
    MobileConfig,
    PushConfig,
    PushConfigError,
)
from hermes_mobile.constants import REASONING_EFFORTS
from hermes_mobile.files.extraction import ExtractionError, extract_attachment
from hermes_mobile.files.tool import read_attachment
from hermes_mobile.hermes.api_client import HermesAPIClient
from hermes_mobile.notifications.apns import (
    APNsPermanentError,
    APNsResult,
    APNsTemporaryError,
    APNsTokenProvider,
    APNsUnregisteredError,
    build_payload,
    classify_response,
    create_apns_client,
    encode_apns_jwt,
    send_apns,
)
from hermes_mobile.notifications.worker import PushWorker
from hermes_mobile.notifications.plaintext import (
    markdown_to_text,
    plain_notification_text,
)
from hermes_mobile.persistence.repositories import (
    ControlStore,
    ProfileStore,
    secret_hash,
)
from hermes_mobile.runtime import MobileRuntime
from hermes_mobile.security.tokens import SecretBox


def _test_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _credentials(pem: str) -> APNsCredentials:
    return APNsCredentials(
        team_id="TEAM123456",
        key_id="KEY1234567",
        topic="app.hermes.mobile",
        private_key_pem=pem,
    )


def _register_apns(store: ControlStore, box: SecretBox, device_id: str, token: str, env="sandbox"):
    store.update_device(
        device_id,
        {
            "push_provider": "apns",
            "push_environment": env,
            "push_token_encrypted": box.encrypt(token),
            "push_token_hash": secret_hash(token),
        },
    )


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


def test_push_config_requires_complete_apns_credentials():
    assert PushConfig(enabled=False).configuration_error() is None
    missing = PushConfig(enabled=True).configuration_error()
    assert missing is not None and "team_id" in missing
    broken = PushConfig(
        enabled=True,
        team_id="TEAM",
        key_id="KEY",
        topic="app.hermes.mobile",
        private_key="not-a-pem",
    )
    with pytest.raises(PushConfigError):
        broken.validate()
    secret = "super-secret-pem-value"
    assert secret not in repr(PushConfig(enabled=True, private_key=secret))
    _credentials(_test_key_pem())
    good = PushConfig(
        enabled=True,
        team_id="TEAM",
        key_id="KEY",
        topic="app.hermes.mobile",
        private_key=_test_key_pem(),
    )
    good.validate()
    assert (
        PushConfig(enabled=True, provider="expo", private_key=_test_key_pem())
        .configuration_error()
        is not None
    )


def test_apns_jwt_is_es256_verifiable_and_renews_before_an_hour():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        encode_dss_signature,
    )
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key,
    )

    pem = _test_key_pem()
    credentials = _credentials(pem)
    now = 1_800_000_000
    token = encode_apns_jwt(credentials, now)
    header_b64, claims_b64, signature_b64 = token.split(".")

    def _decode(segment: str) -> dict:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(__import__("base64").urlsafe_b64decode(padded))

    assert _decode(header_b64) == {"alg": "ES256", "kid": "KEY1234567"}
    assert _decode(claims_b64) == {"iss": "TEAM123456", "iat": now}

    signature = __import__("base64").urlsafe_b64decode(
        signature_b64 + "=" * (-len(signature_b64) % 4)
    )
    assert len(signature) == 64
    der = encode_dss_signature(
        int.from_bytes(signature[:32], "big"),
        int.from_bytes(signature[32:], "big"),
    )
    public_key = load_pem_private_key(pem.encode(), password=None).public_key()
    public_key.verify(
        der,
        f"{header_b64}.{claims_b64}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )

    provider = APNsTokenProvider(credentials, ttl_seconds=3300)
    initial = provider.token(now)
    assert provider.token(now + 3200) == initial
    rotated = provider.token(now + 3301)
    assert rotated != initial
    assert provider.token(now + 3400) == rotated


def test_apns_payload_is_aps_shaped_and_bounded():
    raw = build_payload(
        {
            "title": "Listo",
            "body": "x" * 20_000,
            "data": {
                "type": "run.completed",
                "conversation_id": "conv_1",
                "aps": "ignored",
                "nested": {"ignored": True},
            },
        },
        max_bytes=512,
    )
    assert len(raw) <= 512
    decoded = json.loads(raw)
    assert decoded["aps"]["alert"]["title"] == "Listo"
    assert decoded["aps"]["sound"] == "default"
    assert decoded["conversation_id"] == "conv_1"
    assert "aps" not in {key for key in decoded if key != "aps"}
    assert "nested" not in decoded

    small = json.loads(build_payload({"title": "T", "body": "B", "data": {}}))
    assert small == {"aps": {"alert": {"title": "T", "body": "B"}, "sound": "default"}}

    huge = build_payload(
        {"title": "T" * 10_000, "body": "B", "data": {"conversation_id": "conv_9"}},
        max_bytes=256,
    )
    assert len(huge) <= 256
    decoded_huge = json.loads(huge)
    assert decoded_huge["conversation_id"] == "conv_9"
    assert decoded_huge["aps"]["alert"]


def test_apns_response_classification():
    assert classify_response(200, None) is None
    assert isinstance(classify_response(429, "TooManyRequests"), APNsTemporaryError)
    assert isinstance(classify_response(503, "ServiceUnavailable"), APNsTemporaryError)
    assert isinstance(
        classify_response(403, "ExpiredProviderToken"), APNsTemporaryError
    )
    expired = classify_response(410, "Unregistered", "apns-9")
    assert isinstance(expired, APNsUnregisteredError)
    assert expired.apns_id == "apns-9"
    assert isinstance(classify_response(400, "BadDeviceToken"), APNsPermanentError)
    assert isinstance(classify_response(403, "BadCertificate"), APNsPermanentError)


def test_notification_text_strips_markdown():
    assert plain_notification_text("Hola **mundo** en *cursiva*") == (
        "Hola mundo en cursiva"
    )
    assert plain_notification_text("# Título\n\n- uno\n- dos") == (
        "Título uno dos"
    )
    assert markdown_to_text("`code` y [enlace](https://x.dev) y ![img](a.png)") == (
        "code y enlace y img"
    )
    assert markdown_to_text("~~tachado~~ y __fuerte__ y _énfasis_") == (
        "tachado y fuerte y énfasis"
    )
    assert markdown_to_text("2 * 3 * 4 queda igual") == "2 * 3 * 4 queda igual"
    assert plain_notification_text("```python\nprint('x')\n```") == "print('x')"
    assert plain_notification_text(None) == ""


def test_apns_payload_strips_markdown():
    decoded = json.loads(
        build_payload({"title": "**Listo**", "body": "- Uno\n- *Dos*", "data": {}})
    )
    assert decoded["aps"]["alert"] == {"title": "Listo", "body": "Uno Dos"}


def test_create_apns_client_enables_http2(monkeypatch):
    captured: dict = {}

    class DummyClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("hermes_mobile.notifications.apns.httpx.AsyncClient", DummyClient)
    create_apns_client(12)
    assert captured["http2"] is True
    assert isinstance(captured["timeout"], httpx.Timeout)


@pytest.mark.asyncio
async def test_apns_transport_host_headers_and_errors():
    pem = _test_key_pem()
    credentials = _credentials(pem)
    provider = APNsTokenProvider(credentials)
    captured: list[httpx.Request] = []
    state = {"status": 200, "reason": None}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if state["status"] == 200:
            return httpx.Response(200, headers={"apns-id": "apns-ok"})
        return httpx.Response(
            state["status"], json={"reason": state["reason"]}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        payload = build_payload({"title": "Hola", "body": "Cuerpo", "data": {"run_id": "r1"}})
        result = await send_apns(
            client, credentials, provider, "aabbccdd", "sandbox", payload
        )
        assert result == APNsResult(apns_id="apns-ok")
        request = captured[-1]
        assert request.url.host == "api.sandbox.push.apple.com"
        assert request.url.path == "/3/device/aabbccdd"
        assert request.headers["apns-topic"] == "app.hermes.mobile"
        assert request.headers["apns-push-type"] == "alert"
        assert request.headers["apns-priority"] == "10"
        assert request.headers["authorization"].startswith("bearer ")
        assert json.loads(request.content)["aps"]["alert"]["title"] == "Hola"

        await send_apns(
            client, credentials, provider, "aabbccdd", "production", payload
        )
        assert captured[-1].url.host == "api.push.apple.com"

        state.update(status=410, reason="Unregistered")
        with pytest.raises(APNsUnregisteredError):
            await send_apns(
                client, credentials, provider, "aabbccdd", "sandbox", payload
            )
        state.update(status=429, reason="TooManyRequests")
        with pytest.raises(APNsTemporaryError):
            await send_apns(
                client, credentials, provider, "aabbccdd", "sandbox", payload
            )

        state.update(status=200, reason=None)
        await send_apns(
            client,
            credentials,
            provider,
            "aabbccdd",
            "sandbox",
            payload,
            endpoint_override="https://example.test/3/device/{token}",
        )
        assert captured[-1].url.host == "example.test"
        assert captured[-1].url.path == "/3/device/aabbccdd"

        with pytest.raises(APNsPermanentError):
            await send_apns(
                client, credentials, provider, "aabbccdd", "bogus-env", payload
            )


def _pair_device(store: ControlStore) -> dict:
    pair = store.create_pairing("default", "Alice", 600)
    return store.consume_pairing(
        "default",
        pair["token"],
        {"installation_id": "push-install", "name": "Phone", "platform": "ios"},
        ("devices:self",),
    )


def _worker(store: ControlStore, box: SecretBox, **overrides) -> PushWorker:
    worker = PushWorker(store, box, PushConfig(enabled=True, max_attempts=2, **overrides))
    worker.client = object()
    worker.credentials = object()
    worker.tokens = object()
    return worker


@pytest.mark.asyncio
async def test_push_worker_delivery_and_unregistered_token(tmp_path, monkeypatch):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    paired = _pair_device(store)
    box = SecretBox(tmp_path / "data.key")
    device_id = paired["device"]["id"]
    token = "aa" * 32
    _register_apns(store, box, device_id, token)
    assert store.enqueue_push("default", "run.completed", "run-1", {"title": "Done"}) == 1

    worker = _worker(store, box)

    async def delivered(*_args, **_kwargs):
        return APNsResult(apns_id="apns-ok")

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_apns", delivered)
    await worker._send(store.pending_push()[0])
    with store.connect() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM notification_outbox WHERE dedupe_key='run-1'"
            ).fetchone()
        )
    assert row["status"] == "delivered"
    assert row["provider_ticket_id"] == "apns-ok"
    assert row["delivered_at"] is not None

    store.enqueue_push("default", "run.failed", "run-2", {"title": "Failed"})

    async def gone(*_args, **_kwargs):
        raise APNsUnregisteredError("Unregistered", status=410, apns_id="apns-gone")

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_apns", gone)
    await worker._send(store.pending_push()[0])
    device = store.get_device(device_id)
    assert device["push_token_encrypted"] is None
    assert device["push_environment"] is None
    with store.connect() as conn:
        failed = dict(
            conn.execute(
                "SELECT * FROM notification_outbox WHERE dedupe_key='run-2'"
            ).fetchone()
        )
    assert failed["status"] == "failed" and failed["last_error_code"] == "Unregistered"


@pytest.mark.asyncio
async def test_stale_410_does_not_revoke_a_renewed_token(tmp_path, monkeypatch):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    paired = _pair_device(store)
    box = SecretBox(tmp_path / "data.key")
    device_id = paired["device"]["id"]
    old_token = "bb" * 32
    new_token = "cc" * 32
    _register_apns(store, box, device_id, old_token)
    store.enqueue_push("default", "run.completed", "run-old", {"title": "Done"})
    row = store.pending_push()[0]
    # The device rotates to a fresh token while the old delivery is in flight.
    _register_apns(store, box, device_id, new_token)

    worker = _worker(store, box)
    assert box.decrypt(row["push_token_encrypted"]) == old_token

    async def gone(*_args, **_kwargs):
        raise APNsUnregisteredError("Unregistered", status=410)

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_apns", gone)
    await worker._send(row)
    device = store.get_device(device_id)
    assert device["push_token_encrypted"] is not None
    assert box.decrypt(device["push_token_encrypted"]) == new_token


@pytest.mark.asyncio
async def test_push_worker_temporary_retry_and_attempt_limit(tmp_path, monkeypatch):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    paired = _pair_device(store)
    box = SecretBox(tmp_path / "data.key")
    device_id = paired["device"]["id"]
    _register_apns(store, box, device_id, "dd" * 32)
    store.enqueue_push("default", "run.completed", "retry", {"title": "Done"})
    worker = _worker(store, box)

    async def temporary(*_args, **_kwargs):
        raise APNsTemporaryError("ServiceUnavailable", status=503)

    monkeypatch.setattr("hermes_mobile.notifications.worker.send_apns", temporary)
    await worker._send(store.pending_push()[0])
    with store.connect() as conn:
        pending = dict(conn.execute("SELECT * FROM notification_outbox").fetchone())
    assert pending["status"] == "pending" and pending["attempts"] == 1
    await worker._send({**pending, "push_provider": "apns", "push_environment": "sandbox"})
    with store.connect() as conn:
        failed = conn.execute("SELECT * FROM notification_outbox").fetchone()
    assert failed["status"] == "failed" and failed["attempts"] == 2


@pytest.mark.asyncio
async def test_push_worker_run_delivers_through_apns_transport(tmp_path, monkeypatch):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    paired = _pair_device(store)
    box = SecretBox(tmp_path / "data.key")
    device_id = paired["device"]["id"]
    token = "ee" * 32
    _register_apns(store, box, device_id, token)
    store.enqueue_push("default", "run.completed", "run-transport", {"title": "Hola"})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"apns-id": "apns-e2e"})

    config = PushConfig(
        enabled=True,
        team_id="TEAM",
        key_id="KEY",
        topic="app.hermes.mobile",
        private_key=_test_key_pem(),
        max_attempts=2,
        endpoint_override="https://apns.test/3/device/{token}",
    )
    worker = PushWorker(store, box, config)
    monkeypatch.setattr(
        "hermes_mobile.notifications.worker.create_apns_client",
        lambda *_args, **_kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    task = asyncio.create_task(worker.run())
    try:
        for _ in range(100):
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT status FROM notification_outbox "
                    "WHERE dedupe_key='run-transport'"
                ).fetchone()
            if row and row[0] == "delivered":
                break
            await asyncio.sleep(0.02)
        assert row[0] == "delivered"
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await worker.close()
    assert seen
    assert seen[0].url.path == f"/3/device/{token}"
    assert seen[0].headers["apns-topic"] == "app.hermes.mobile"
    assert seen[0].headers["authorization"].startswith("bearer ")
    assert json.loads(seen[0].content)["aps"]["alert"]["title"] == "Hola"


def test_legacy_expo_registrations_never_reach_apns(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    paired = _pair_device(store)
    box = SecretBox(tmp_path / "data.key")
    device_id = paired["device"]["id"]
    store.update_device(
        device_id,
        {"push_provider": "expo", "push_token_encrypted": box.encrypt("ExponentPushToken[x]")},
    )
    # Re-running initialize simulates upgrading a database with legacy rows.
    store.initialize()
    device = store.get_device(device_id)
    assert device["push_provider"] is None
    assert device["push_token_encrypted"] is None
    assert store.enqueue_push("default", "run.completed", "legacy", {"title": "x"}) == 0



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
async def test_runtime_preloads_profiles_with_active_mobile_devices(tmp_path):
    class Facade:
        def __init__(self):
            self.reconciled = asyncio.Event()

        async def start(self):
            pass

        async def close(self):
            pass

        async def list_scheduled_tasks(self, profile, include_disabled=False):
            assert profile == "mujer"
            assert include_disabled is True
            self.reconciled.set()
            return {"jobs": []}

    facade = Facade()
    runtime = MobileRuntime(
        MobileConfig(default_home=tmp_path, push=PushConfig(enabled=False)), facade
    )
    runtime.control.initialize()
    pairing = runtime.control.create_pairing("mujer", "Mujer", 600)
    runtime.control.consume_pairing(
        "mujer",
        pairing["token"],
        {
            "installation_id": "installation-preload",
            "name": "Test phone",
            "platform": "ios",
        },
        ("conversations:read",),
    )

    await runtime.start()
    try:
        assert "mujer" in runtime._stores
        await asyncio.wait_for(facade.reconciled.wait(), timeout=1)
    finally:
        await runtime.close()


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

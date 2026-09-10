from __future__ import annotations

import sqlite3
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_mobile.api import admin
from hermes_mobile.api.admin import ADMIN_COOKIE, AdminHTTPError, AdminPortal
from hermes_mobile.api.routes import MobileAPI
from hermes_mobile.config import MobileConfig, PushConfig
from hermes_mobile.runtime import MobileRuntime

ORIGIN = "https://hermes.example.com"


def _cookie(response) -> str:
    parsed = SimpleCookie()
    parsed.load(response.headers["Set-Cookie"])
    return parsed[ADMIN_COOKIE].value


@pytest.fixture
async def admin_client(tmp_path, fake_facade):
    (tmp_path / "profiles" / "mujer").mkdir(parents=True)
    (tmp_path / "profiles" / "oculto").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "gateway": {
                    "multiplex_profiles": True,
                    "multiplex_profile_allowlist": ["mujer"],
                }
            }
        )
    )
    runtime = MobileRuntime(
        MobileConfig(
            default_home=tmp_path,
            public_base_url=ORIGIN,
            push=PushConfig(enabled=False),
        ),
        facade=fake_facade,
    )
    app = web.Application()
    MobileAPI(runtime).wire(app)
    async with TestClient(TestServer(app)) as client:
        yield client, runtime


async def test_root_is_the_unscoped_admin_portal(admin_client):
    client, _runtime = admin_client
    root = await client.get("/")
    assert root.status == 200
    portal_html = await root.text()
    assert "Hermes Mobile" in portal_html
    assert "No se seleccionó ninguna passkey" in portal_html
    assert "frame-ancestors 'none'" in root.headers["Content-Security-Policy"]
    assert root.headers["Cache-Control"] == "no-store"

    redirect = await client.get("/v1/mobile/admin", allow_redirects=False)
    assert redirect.status == 303 and redirect.headers["Location"] == "/"

    status = await client.get("/v1/mobile/admin/status")
    assert await status.json() == {"configured": False, "authenticated": False}


async def test_passkey_registration_login_and_cross_profile_pairing(
    admin_client, monkeypatch
):
    client, runtime = admin_client
    bootstrap = runtime.control.create_admin_bootstrap()
    origin_headers = {"Origin": ORIGIN}

    start = await client.post(
        "/v1/mobile/admin/register/options",
        headers=origin_headers,
        json={"bootstrap_token": bootstrap["token"]},
    )
    assert start.status == 200
    registration = await start.json()
    assert registration["publicKey"]["rp"]["id"] == "hermes.example.com"
    assert (
        registration["publicKey"]["authenticatorSelection"]["userVerification"]
        == "required"
    )

    credential_id = b"credential-1"
    monkeypatch.setattr(
        admin,
        "verify_registration_response",
        lambda **_kwargs: SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"public-key",
            sign_count=0,
        ),
    )
    finish = await client.post(
        "/v1/mobile/admin/register/verify",
        headers=origin_headers,
        json={
            "bootstrap_token": bootstrap["token"],
            "ceremony_id": registration["ceremony_id"],
            "credential": {
                "id": "Y3JlZGVudGlhbC0x",
                "rawId": "Y3JlZGVudGlhbC0x",
                "type": "public-key",
                "response": {"transports": ["internal"]},
            },
        },
    )
    assert finish.status == 201
    finish_payload = await finish.json()
    assert finish_payload["profiles"] == ["default", "mujer"]
    assert "oculto" not in finish_payload["profiles"]
    assert "Secure" in finish.headers["Set-Cookie"]
    assert "HttpOnly" in finish.headers["Set-Cookie"]
    assert "SameSite=Strict" in finish.headers["Set-Cookie"]
    first_cookie = _cookie(finish)

    second_bootstrap = runtime.control.create_admin_bootstrap()
    with pytest.raises(sqlite3.IntegrityError):
        runtime.control.register_admin_credential(
            second_bootstrap["token"], b"credential-2", b"key-2", 0, []
        )
    assert len(runtime.control.admin_credentials()) == 1

    replay = await client.post(
        "/v1/mobile/admin/register/options",
        headers=origin_headers,
        json={"bootstrap_token": bootstrap["token"]},
    )
    assert replay.status == 409

    status = await client.get(
        "/v1/mobile/admin/status",
        headers={"Cookie": f"{ADMIN_COOKIE}={first_cookie}"},
    )
    status_payload = await status.json()
    assert status_payload["authenticated"] is True
    assert status_payload["profiles"] == ["default", "mujer"]
    assert status_payload["csrf_token"]

    login_start = await client.post(
        "/v1/mobile/admin/login/options", headers=origin_headers, json={}
    )
    login_options = await login_start.json()
    assert login_options["publicKey"]["rpId"] == "hermes.example.com"
    assert login_options["publicKey"]["userVerification"] == "required"
    assert login_options["publicKey"]["allowCredentials"][0]["id"]

    monkeypatch.setattr(
        admin,
        "verify_authentication_response",
        lambda **_kwargs: SimpleNamespace(new_sign_count=2),
    )
    login_finish = await client.post(
        "/v1/mobile/admin/login/verify",
        headers=origin_headers,
        json={
            "ceremony_id": login_options["ceremony_id"],
            "credential": {
                "id": "Y3JlZGVudGlhbC0x",
                "rawId": "Y3JlZGVudGlhbC0x",
                "type": "public-key",
                "response": {},
            },
        },
    )
    assert login_finish.status == 201
    login_payload = await login_finish.json()
    login_cookie = _cookie(login_finish)
    auth_headers = {
        "Cookie": f"{ADMIN_COOKIE}={login_cookie}",
        "Origin": ORIGIN,
        "X-CSRF-Token": login_payload["csrf_token"],
    }

    qr = await client.post(
        "/v1/mobile/admin/pairings",
        headers=auth_headers,
        json={"profile": "mujer", "display_name": "Phone"},
    )
    assert qr.status == 200
    assert qr.content_type == "image/svg+xml"
    assert qr.headers["X-Pairing-Profile"] == "mujer"
    assert b"<svg" in await qr.read()
    with runtime.control.connect() as connection:
        row = connection.execute(
            "SELECT profile_id FROM pairing_tokens ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row["profile_id"] == "mujer"

    hidden = await client.post(
        "/v1/mobile/admin/pairings",
        headers=auth_headers,
        json={"profile": "oculto"},
    )
    assert hidden.status == 400


async def test_admin_mutations_require_session_csrf_and_exact_origin(admin_client):
    client, runtime = admin_client
    runtime.control.add_admin_credential(b"id", b"key", 0, [])

    unauthenticated = await client.post(
        "/v1/mobile/admin/pairings",
        headers={"Origin": ORIGIN},
        json={"profile": "default"},
    )
    assert unauthenticated.status == 401

    session = runtime.control.create_admin_session()
    cookie = {"Cookie": f"{ADMIN_COOKIE}={session['token']}"}
    cross_origin = await client.post(
        "/v1/mobile/admin/pairings",
        headers={
            **cookie,
            "Origin": "https://attacker.example",
            "X-CSRF-Token": session["csrf"],
        },
        json={"profile": "default"},
    )
    assert cross_origin.status == 403

    missing_csrf = await client.post(
        "/v1/mobile/admin/pairings",
        headers={**cookie, "Origin": ORIGIN},
        json={"profile": "default"},
    )
    assert missing_csrf.status == 403

    logout = await client.post(
        "/v1/mobile/admin/logout",
        headers={
            **cookie,
            "Origin": ORIGIN,
            "X-CSRF-Token": session["csrf"],
        },
    )
    assert logout.status == 200
    assert (await logout.json()) == {"authenticated": False}
    assert "Max-Age=0" in logout.headers["Set-Cookie"]


async def test_admin_auth_errors_are_bounded_and_non_revealing(
    admin_client, monkeypatch
):
    client, runtime = admin_client
    origin = {"Origin": ORIGIN}

    not_configured = await client.post(
        "/v1/mobile/admin/login/options", headers=origin, json={}
    )
    assert not_configured.status == 409
    wrong_origin = await client.post(
        "/v1/mobile/admin/register/options",
        headers={"Origin": "https://attacker.example"},
        json={},
    )
    assert wrong_origin.status == 403
    invalid_json = await client.post(
        "/v1/mobile/admin/register/options",
        headers={**origin, "Content-Type": "application/json"},
        data="not-json",
    )
    assert invalid_json.status == 400
    invalid_shape = await client.post(
        "/v1/mobile/admin/register/options", headers=origin, json=[]
    )
    assert invalid_shape.status == 400
    invalid_bootstrap = await client.post(
        "/v1/mobile/admin/register/options",
        headers=origin,
        json={"bootstrap_token": "not-valid"},
    )
    assert invalid_bootstrap.status == 401

    bootstrap = runtime.control.create_admin_bootstrap()
    missing_credential = await client.post(
        "/v1/mobile/admin/register/verify",
        headers=origin,
        json={"bootstrap_token": bootstrap["token"]},
    )
    assert missing_credential.status == 400

    runtime.control.add_admin_credential(b"known", b"key", 0, ["unsupported-transport"])
    missing_login_credential = await client.post(
        "/v1/mobile/admin/login/verify", headers=origin, json={}
    )
    assert missing_login_credential.status == 400
    login_start = await client.post(
        "/v1/mobile/admin/login/options", headers=origin, json={}
    )
    login_options = await login_start.json()
    unknown_credential = await client.post(
        "/v1/mobile/admin/login/verify",
        headers=origin,
        json={
            "ceremony_id": login_options["ceremony_id"],
            "credential": {
                "id": "dW5rbm93bg",
                "rawId": "dW5rbm93bg",
                "type": "public-key",
                "response": {},
            },
        },
    )
    assert unknown_credential.status == 401

    monkeypatch.setattr(
        runtime.control,
        "admin_is_configured",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    internal = await client.get("/v1/mobile/admin/status")
    assert internal.status == 500
    assert (await internal.json())["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    "value",
    [
        "http://hermes.example.com",
        "https://user@hermes.example.com",
        "https://hermes.example.com/path",
        "https://hermes.example.com?query=1",
        "https://hermes.example.com#fragment",
    ],
)
def test_admin_portal_rejects_non_origin_public_urls(value):
    portal = AdminPortal(SimpleNamespace(config=SimpleNamespace(public_base_url=value)))
    with pytest.raises(AdminHTTPError) as exc:
        _ = portal._origin
    assert exc.value.code == "portal_unavailable"

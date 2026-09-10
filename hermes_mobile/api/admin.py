from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import qrcode
import qrcode.image.svg
import yaml
from aiohttp import web
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..ids import new_id
from ..persistence.repositories import InvalidAdminAuth
from ..runtime import MobileRuntime

logger = logging.getLogger("hermes_mobile.admin")

ADMIN_PREFIX = "/v1/mobile/admin"
ADMIN_COOKIE = "__Host-hermes_mobile_admin"


class AdminHTTPError(RuntimeError):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AdminPortal:
    def __init__(self, runtime: MobileRuntime):
        self.runtime = runtime
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def wire(self, app: web.Application) -> None:
        app.router.add_get("/", self._wrap(self.portal))
        app.router.add_get(ADMIN_PREFIX, self._wrap(self.redirect_to_portal))
        app.router.add_get(ADMIN_PREFIX + "/status", self._wrap(self.status))
        app.router.add_post(
            ADMIN_PREFIX + "/register/options", self._wrap(self.register_options)
        )
        app.router.add_post(
            ADMIN_PREFIX + "/register/verify", self._wrap(self.register_verify)
        )
        app.router.add_post(
            ADMIN_PREFIX + "/login/options", self._wrap(self.login_options)
        )
        app.router.add_post(
            ADMIN_PREFIX + "/login/verify", self._wrap(self.login_verify)
        )
        app.router.add_post(ADMIN_PREFIX + "/pairings", self._wrap(self.pairing))
        app.router.add_post(ADMIN_PREFIX + "/logout", self._wrap(self.logout))

    def _wrap(
        self, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            request_id = new_id("req")
            try:
                response = await handler(request)
            except web.HTTPException:
                raise
            except AdminHTTPError as exc:
                response = web.json_response(
                    {"error": {"code": exc.code, "message": exc.message}},
                    status=exc.status,
                )
            except InvalidAdminAuth as exc:
                response = web.json_response(
                    {
                        "error": {
                            "code": str(exc),
                            "message": "La autorización administrativa no es válida.",
                        }
                    },
                    status=401,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Unhandled admin portal error request_id=%s", request_id
                )
                response = web.json_response(
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "No se pudo completar la operación.",
                        }
                    },
                    status=500,
                )
            response.headers["X-Request-Id"] = request_id
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return wrapped

    @property
    def _origin(self) -> str:
        parsed = urlparse(self.runtime.config.public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise AdminHTTPError(
                "portal_unavailable",
                "El portal requiere public_base_url como origen HTTPS, sin ruta.",
                503,
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def _rp_id(self) -> str:
        hostname = urlparse(self._origin).hostname
        if not hostname:
            raise AdminHTTPError("portal_unavailable", "Dominio no válido.", 503)
        return hostname

    def _rate_limit(
        self, request: web.Request, limit: int = 12, bucket: str = "auth"
    ) -> None:
        key = f"{bucket}:{request.remote or 'unknown'}"
        now = time.monotonic()
        if len(self._attempts) >= 4096 and key not in self._attempts:
            for candidate, values in list(self._attempts.items()):
                while values and now - values[0] > 60:
                    values.popleft()
                if not values:
                    self._attempts.pop(candidate, None)
            while len(self._attempts) >= 4096:
                self._attempts.pop(next(iter(self._attempts)))
        attempts = self._attempts[key]
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= limit:
            raise AdminHTTPError(
                "rate_limited", "Demasiados intentos. Espera un minuto.", 429
            )
        attempts.append(now)

    def _require_origin(self, request: web.Request) -> None:
        if request.headers.get("Origin") != self._origin:
            raise AdminHTTPError("invalid_origin", "Origen no permitido.", 403)

    async def _json_body(self, request: web.Request) -> dict[str, Any]:
        try:
            value = await request.json()
        except Exception as exc:
            raise AdminHTTPError(
                "invalid_request", "El body debe ser JSON válido.", 400
            ) from exc
        if not isinstance(value, dict):
            raise AdminHTTPError("invalid_request", "Body no válido.", 400)
        return value

    async def _session(self, request: web.Request) -> dict[str, Any] | None:
        token = request.cookies.get(ADMIN_COOKIE, "")
        return await asyncio.to_thread(self.runtime.control.admin_session, token)

    async def _require_session(self, request: web.Request) -> dict[str, Any]:
        session = await self._session(request)
        if not session:
            raise AdminHTTPError("authentication_required", "Inicia sesión.", 401)
        return session

    async def _require_csrf(
        self, request: web.Request, session: dict[str, Any]
    ) -> None:
        self._require_origin(request)
        csrf = request.headers.get("X-CSRF-Token", "")
        valid = await asyncio.to_thread(
            self.runtime.control.verify_admin_csrf, session, csrf
        )
        if not valid:
            raise AdminHTTPError("invalid_csrf", "Petición no autorizada.", 403)

    def _profiles(self) -> list[str]:
        root = self.runtime.config.default_home
        try:
            config = yaml.safe_load((root / "config.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError):
            config = {}
        gateway = config.get("gateway") if isinstance(config, dict) else {}
        gateway = gateway if isinstance(gateway, dict) else {}
        allowlist = gateway.get("multiplex_profile_allowlist")
        allowed = (
            {str(value) for value in allowlist} if isinstance(allowlist, list) else None
        )
        names = ["default"]
        profiles_dir = root / "profiles"
        if profiles_dir.is_dir():
            names.extend(
                sorted(path.name for path in profiles_dir.iterdir() if path.is_dir())
            )
        return [
            name
            for name in dict.fromkeys(names)
            if name == "default" or allowed is None or name in allowed
        ]

    def _credential_descriptors(self) -> list[PublicKeyCredentialDescriptor]:
        descriptors = []
        for row in self.runtime.control.admin_credentials():
            transports = []
            for value in json.loads(row["transports_json"] or "[]"):
                try:
                    transports.append(AuthenticatorTransport(value))
                except ValueError:
                    continue
            descriptors.append(
                PublicKeyCredentialDescriptor(
                    id=bytes(row["credential_id"]), transports=transports or None
                )
            )
        return descriptors

    async def _session_response(self, payload: dict[str, Any]) -> web.Response:
        session = await asyncio.to_thread(self.runtime.control.create_admin_session)
        response = web.json_response(
            {**payload, "csrf_token": session["csrf"]}, status=201
        )
        response.set_cookie(
            ADMIN_COOKIE,
            session["token"],
            max_age=3600,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def portal(self, _request: web.Request) -> web.Response:
        nonce = secrets.token_urlsafe(18)
        response = web.Response(
            text=PORTAL_HTML.replace("__CSP_NONCE__", nonce),
            content_type="text/html",
            charset="utf-8",
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "img-src 'self' blob:; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=()"
        return response

    async def redirect_to_portal(self, _request: web.Request) -> web.Response:
        response = web.HTTPSeeOther("/")
        response.headers["Cache-Control"] = "no-store"
        raise response

    async def status(self, request: web.Request) -> web.Response:
        configured = await asyncio.to_thread(self.runtime.control.admin_is_configured)
        session = await self._session(request) if configured else None
        payload: dict[str, Any] = {
            "configured": configured,
            "authenticated": bool(session),
        }
        if session:
            payload["profiles"] = self._profiles()
            payload["csrf_token"] = session["csrf_token"]
        return web.json_response(payload)

    async def register_options(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        self._require_origin(request)
        if await asyncio.to_thread(self.runtime.control.admin_is_configured):
            raise AdminHTTPError("already_configured", "Ya existe una passkey.", 409)
        body = await self._json_body(request)
        bootstrap = str(body.get("bootstrap_token") or "")
        if not await asyncio.to_thread(
            self.runtime.control.valid_admin_bootstrap, bootstrap
        ):
            raise AdminHTTPError("invalid_bootstrap", "Enlace no válido.", 401)
        challenge = await asyncio.to_thread(
            self.runtime.control.create_admin_challenge, "registration"
        )
        user_handle = await asyncio.to_thread(self.runtime.control.admin_user_handle)
        options = generate_registration_options(
            rp_id=self._rp_id,
            rp_name="Hermes Mobile",
            user_id=user_handle,
            user_name="owner",
            user_display_name="Hermes owner",
            challenge=challenge["challenge"],
            timeout=120_000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return web.json_response(
            {
                "ceremony_id": challenge["id"],
                "publicKey": json.loads(options_to_json(options)),
            }
        )

    async def register_verify(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        self._require_origin(request)
        if await asyncio.to_thread(self.runtime.control.admin_is_configured):
            raise AdminHTTPError("already_configured", "Ya existe una passkey.", 409)
        body = await self._json_body(request)
        bootstrap = str(body.get("bootstrap_token") or "")
        if not await asyncio.to_thread(
            self.runtime.control.valid_admin_bootstrap, bootstrap
        ):
            raise AdminHTTPError("invalid_bootstrap", "Enlace no válido.", 401)
        credential = body.get("credential")
        if not isinstance(credential, dict):
            raise AdminHTTPError("invalid_request", "Falta la credencial.", 400)
        challenge = await asyncio.to_thread(
            self.runtime.control.consume_admin_challenge,
            str(body.get("ceremony_id") or ""),
            "registration",
        )
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except Exception as exc:
            raise AdminHTTPError(
                "invalid_passkey", "No se pudo verificar la passkey.", 401
            ) from exc
        transports = credential.get("response", {}).get("transports", [])
        transports = transports if isinstance(transports, list) else []
        try:
            await asyncio.to_thread(
                self.runtime.control.register_admin_credential,
                bootstrap,
                verified.credential_id,
                verified.credential_public_key,
                verified.sign_count,
                [str(value) for value in transports],
            )
        except sqlite3.IntegrityError as exc:
            raise AdminHTTPError(
                "credential_exists", "Passkey duplicada.", 409
            ) from exc
        await asyncio.to_thread(
            self.runtime.control.audit_admin,
            "passkey.registered",
            request.remote or "",
        )
        return await self._session_response(
            {"authenticated": True, "profiles": self._profiles()}
        )

    async def login_options(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        self._require_origin(request)
        if not await asyncio.to_thread(self.runtime.control.admin_is_configured):
            raise AdminHTTPError("not_configured", "No existe una passkey.", 409)
        challenge = await asyncio.to_thread(
            self.runtime.control.create_admin_challenge, "authentication"
        )
        options = generate_authentication_options(
            rp_id=self._rp_id,
            challenge=challenge["challenge"],
            timeout=120_000,
            allow_credentials=self._credential_descriptors(),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return web.json_response(
            {
                "ceremony_id": challenge["id"],
                "publicKey": json.loads(options_to_json(options)),
            }
        )

    async def login_verify(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        self._require_origin(request)
        body = await self._json_body(request)
        credential = body.get("credential")
        if not isinstance(credential, dict):
            raise AdminHTTPError("invalid_request", "Falta la credencial.", 400)
        challenge = await asyncio.to_thread(
            self.runtime.control.consume_admin_challenge,
            str(body.get("ceremony_id") or ""),
            "authentication",
        )
        try:
            credential_id = base64url_to_bytes(
                str(credential.get("rawId") or credential.get("id") or "")
            )
        except Exception as exc:
            raise AdminHTTPError("invalid_passkey", "Passkey no válida.", 401) from exc
        stored = await asyncio.to_thread(
            self.runtime.control.admin_credential, credential_id
        )
        if not stored:
            raise AdminHTTPError("invalid_passkey", "Passkey no válida.", 401)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=bytes(stored["public_key"]),
                credential_current_sign_count=int(stored["sign_count"]),
                require_user_verification=True,
            )
        except Exception as exc:
            raise AdminHTTPError(
                "invalid_passkey", "No se pudo verificar la passkey.", 401
            ) from exc
        await asyncio.to_thread(
            self.runtime.control.update_admin_credential,
            credential_id,
            verified.new_sign_count,
        )
        await asyncio.to_thread(
            self.runtime.control.audit_admin,
            "passkey.authenticated",
            request.remote or "",
        )
        return await self._session_response(
            {"authenticated": True, "profiles": self._profiles()}
        )

    async def pairing(self, request: web.Request) -> web.Response:
        self._rate_limit(request, limit=6, bucket="pairing")
        session = await self._require_session(request)
        await self._require_csrf(request, session)
        body = await self._json_body(request)
        profile = str(body.get("profile") or "")
        if profile not in self._profiles():
            raise AdminHTTPError("invalid_profile", "Perfil no disponible.", 400)
        display_name = str(body.get("display_name") or profile).strip()[:80] or profile
        pairing = await asyncio.to_thread(
            self.runtime.control.create_pairing,
            profile,
            display_name,
            self.runtime.config.pairing_ttl_seconds,
        )
        params = {
            "profile": profile,
            "token": pairing["token"],
            "base_url": self.runtime.config.public_base_url,
        }
        pairing_url = f"hermes://pair?{urlencode(params)}"
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=12,
            border=4,
        )
        qr.add_data(pairing_url)
        qr.make(fit=True)
        output = io.BytesIO()
        qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(output)
        await asyncio.to_thread(
            self.runtime.control.audit_admin,
            "pairing.created",
            request.remote or "",
            {"profile": profile},
        )
        return web.Response(
            body=output.getvalue(),
            content_type="image/svg+xml",
            headers={
                "X-Pairing-Profile": profile,
                "X-Pairing-Expires-At": pairing["expires_at"],
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    async def logout(self, request: web.Request) -> web.Response:
        session = await self._require_session(request)
        await self._require_csrf(request, session)
        token = request.cookies.get(ADMIN_COOKIE, "")
        await asyncio.to_thread(self.runtime.control.revoke_admin_session, token)
        response = web.json_response({"authenticated": False})
        response.del_cookie(ADMIN_COOKIE, path="/")
        return response


PORTAL_HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Hermes Mobile</title>
  <style nonce="__CSP_NONCE__">
    :root { font-family: ui-sans-serif, system-ui, sans-serif; color: #f4efe6; background: #071312; }
    * { box-sizing: border-box; }
    body { min-height: 100vh; margin: 0; display: grid; place-items: center; padding: 24px; }
    main { width: min(100%, 460px); background: #102321; border: 1px solid #2a4b47; border-radius: 22px; padding: 28px; box-shadow: 0 24px 80px #0008; }
    h1 { margin: 0 0 8px; font-size: 1.8rem; }
    p { color: #b9cbc7; line-height: 1.5; }
    section[hidden] { display: none; }
    label { display: grid; gap: 8px; margin: 20px 0; color: #dce7e4; }
    select, button { width: 100%; min-height: 48px; border-radius: 12px; border: 1px solid #416963; font: inherit; }
    select { padding: 0 14px; color: #f4efe6; background: #091a18; }
    button { padding: 0 18px; color: #061210; background: #79e5cf; font-weight: 750; cursor: pointer; }
    button.secondary { color: #dce7e4; background: transparent; margin-top: 12px; }
    button:disabled { opacity: .55; cursor: wait; }
    #qr { display: block; width: min(100%, 380px); margin: 24px auto 8px; border-radius: 14px; background: white; }
    #message { min-height: 24px; color: #ffcf70; }
    .eyebrow { color: #79e5cf; text-transform: uppercase; letter-spacing: .14em; font-size: .75rem; font-weight: 800; }
  </style>
</head>
<body>
<main>
  <div class="eyebrow">Conexión segura</div>
  <h1>Hermes Mobile</h1>
  <p id="message">Comprobando el servidor…</p>
  <section id="setup" hidden>
    <p>Registra una passkey para proteger la creación de nuevos emparejamientos.</p>
    <button id="setupButton">Registrar passkey</button>
  </section>
  <section id="login" hidden>
    <p>Usa Face ID, Touch ID, huella o el PIN de tu dispositivo.</p>
    <button id="loginButton">Entrar con passkey</button>
  </section>
  <section id="portal" hidden>
    <label>Perfil de Hermes
      <select id="profile"></select>
    </label>
    <button id="pairButton">Generar QR de emparejamiento</button>
    <img id="qr" alt="QR de emparejamiento" hidden>
    <p id="expiry"></p>
    <button class="secondary" id="logoutButton">Cerrar sesión</button>
  </section>
</main>
<script nonce="__CSP_NONCE__">
const API = '/v1/mobile/admin';
const message = document.querySelector('#message');
const setup = document.querySelector('#setup');
const login = document.querySelector('#login');
const portal = document.querySelector('#portal');
const profile = document.querySelector('#profile');
const qr = document.querySelector('#qr');
const expiry = document.querySelector('#expiry');
let csrf = '';
let qrObjectUrl = '';

const decode = value => {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - base64.length % 4) % 4);
  return Uint8Array.from(atob(padded), char => char.charCodeAt(0));
};
const encode = value => btoa(String.fromCharCode(...new Uint8Array(value)))
  .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const publicKeyOptions = value => {
  value.challenge = decode(value.challenge);
  if (value.user) value.user.id = decode(value.user.id);
  for (const item of value.allowCredentials || []) item.id = decode(item.id);
  for (const item of value.excludeCredentials || []) item.id = decode(item.id);
  return value;
};
const credentialJSON = credential => ({
  id: credential.id,
  rawId: encode(credential.rawId),
  type: credential.type,
  response: {
    clientDataJSON: encode(credential.response.clientDataJSON),
    attestationObject: credential.response.attestationObject ? encode(credential.response.attestationObject) : undefined,
    authenticatorData: credential.response.authenticatorData ? encode(credential.response.authenticatorData) : undefined,
    signature: credential.response.signature ? encode(credential.response.signature) : undefined,
    userHandle: credential.response.userHandle ? encode(credential.response.userHandle) : undefined,
    transports: credential.response.getTransports ? credential.response.getTransports() : undefined,
  },
  clientExtensionResults: credential.getClientExtensionResults(),
});
const requirePasskeySupport = () => {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    throw new Error('Este navegador no admite passkeys. Usa Safari, Chrome o Firefox actualizado.');
  }
};
const requireCredential = credential => {
  if (!credential) {
    throw new Error('No se seleccionó ninguna passkey. Vuelve a intentarlo y completa la verificación del dispositivo.');
  }
  return credential;
};
const friendlyError = error => {
  if (error?.name === 'NotAllowedError') {
    return `No se completó la autenticación. Vuelve a intentarlo y selecciona la passkey de ${location.hostname}.`;
  }
  if (error?.name === 'SecurityError') {
    return `El navegador rechazó la passkey por seguridad. Comprueba que estás en https://${location.host}.`;
  }
  return error?.message || 'No se pudo completar la operación.';
};
async function jsonRequest(path, options = {}) {
  const response = await fetch(API + path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || 'No se pudo completar la operación.');
  return body;
}
function show(target, text) {
  setup.hidden = target !== setup;
  login.hidden = target !== login;
  portal.hidden = target !== portal;
  message.textContent = text;
}
function openPortal(profiles, text = 'Sesión protegida con passkey.') {
  profile.replaceChildren(...profiles.map(name => new Option(name, name)));
  show(portal, text);
}
async function refresh() {
  const state = await jsonRequest('/status');
  if (state.authenticated) {
    csrf = state.csrf_token;
    return openPortal(state.profiles);
  }
  if (state.configured) return show(login, 'Autentícate para generar un QR.');
  const token = new URLSearchParams(location.hash.slice(1)).get('setup');
  if (token) return show(setup, 'Completa la configuración inicial.');
  show(null, 'El portal aún no tiene una passkey. Usa un enlace de inicialización válido.');
}
async function registerPasskey() {
  requirePasskeySupport();
  const token = new URLSearchParams(location.hash.slice(1)).get('setup');
  const start = await jsonRequest('/register/options', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bootstrap_token: token}),
  });
  const credential = requireCredential(await navigator.credentials.create({publicKey: publicKeyOptions(start.publicKey)}));
  const complete = await jsonRequest('/register/verify', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bootstrap_token: token, ceremony_id: start.ceremony_id, credential: credentialJSON(credential)}),
  });
  history.replaceState(null, '', location.pathname);
  csrf = complete.csrf_token;
  openPortal(complete.profiles, 'Passkey registrada correctamente.');
}
async function loginPasskey() {
  requirePasskeySupport();
  const start = await jsonRequest('/login/options', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
  });
  const credential = requireCredential(await navigator.credentials.get({publicKey: publicKeyOptions(start.publicKey)}));
  const complete = await jsonRequest('/login/verify', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ceremony_id: start.ceremony_id, credential: credentialJSON(credential)}),
  });
  csrf = complete.csrf_token;
  openPortal(complete.profiles);
}
async function createPairing() {
  const response = await fetch(API + '/pairings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: JSON.stringify({profile: profile.value, display_name: 'Hermes Mobile'}),
  });
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error?.message || 'No se pudo generar el QR.');
  }
  if (qrObjectUrl) URL.revokeObjectURL(qrObjectUrl);
  qrObjectUrl = URL.createObjectURL(await response.blob());
  qr.src = qrObjectUrl;
  qr.hidden = false;
  const expiresAt = new Date(response.headers.get('X-Pairing-Expires-At'));
  expiry.textContent = `Perfil ${profile.value} · caduca a las ${expiresAt.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}.`;
  message.textContent = 'Escanea este código con Hermes Mobile.';
}
async function run(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { message.textContent = friendlyError(error); }
  finally { button.disabled = false; }
}
document.querySelector('#setupButton').onclick = event => run(event.currentTarget, registerPasskey);
document.querySelector('#loginButton').onclick = event => run(event.currentTarget, loginPasskey);
document.querySelector('#pairButton').onclick = event => run(event.currentTarget, createPairing);
document.querySelector('#logoutButton').onclick = event => run(event.currentTarget, async () => {
  await jsonRequest('/logout', {method: 'POST', headers: {'X-CSRF-Token': csrf}});
  csrf = '';
  await refresh();
});
refresh().catch(error => { message.textContent = error.message; });
</script>
</body>
</html>
"""

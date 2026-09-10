from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web
from pydantic import ValidationError

from ..constants import (
    API_VERSION,
    DEFAULT_SCOPES,
    SUPPORTED_MIME_TYPES,
    TERMINAL_RUN_STATUSES,
)
from ..files.validation import detect_mime, safe_filename
from ..ids import new_id, valid_request_id
from ..persistence.repositories import (
    InvalidPairing,
    InvalidRefresh,
    RefreshReuse,
    StoreError,
    iso,
    json_dump,
)
from ..runtime import MobileRuntime
from ..security.tokens import TokenError
from .admin import AdminPortal
from .errors import MobileError, error_response
from .schemas import (
    ApprovalRequest,
    ConversationCreate,
    ConversationPatch,
    DeviceUpdate,
    ForkRequest,
    PairRequest,
    ReadRequest,
    RefreshRequest,
    RunCreate,
    SteerRequest,
)

logger = logging.getLogger("hermes_mobile.api")

Handler = Callable[[web.Request, dict[str, Any] | None], Awaitable[web.StreamResponse]]
WIRED_KEY = web.AppKey("hermes_mobile_wired", bool)


class MobileAPI:
    PREFIX = "/p/{profile}/v1/mobile"

    def __init__(self, runtime: MobileRuntime):
        self.runtime = runtime
        self.admin = AdminPortal(runtime)
        self._pair_attempts: dict[str, deque[float]] = defaultdict(deque)
        self._sse_counts: dict[str, int] = defaultdict(int)

    def wire(self, app: web.Application, _adapter: Any = None) -> None:
        if app.get(WIRED_KEY):
            return
        app[WIRED_KEY] = True
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        self.admin.wire(app)
        routes: list[tuple[str, str, Handler, str | None]] = [
            ("GET", "/health", self.health, None),
            ("GET", "/capabilities", self.capabilities, "conversations:read"),
            ("GET", "/bootstrap", self.bootstrap, "conversations:read"),
            ("POST", "/auth/pair", self.pair, None),
            ("POST", "/auth/refresh", self.refresh, None),
            ("POST", "/auth/logout", self.logout, "devices:self"),
            ("GET", "/me", self.me, "conversations:read"),
            ("GET", "/devices", self.devices, "devices:self"),
            ("POST", "/devices", self.update_current_device, "devices:self"),
            ("PATCH", "/devices/{device_id}", self.patch_device, "devices:self"),
            ("DELETE", "/devices/{device_id}", self.delete_device, "devices:self"),
            ("GET", "/conversations", self.conversations, "conversations:read"),
            ("POST", "/conversations", self.create_conversation, "conversations:write"),
            (
                "GET",
                "/conversations/{conversation_id}",
                self.get_conversation,
                "conversations:read",
            ),
            (
                "PATCH",
                "/conversations/{conversation_id}",
                self.patch_conversation,
                "conversations:write",
            ),
            (
                "DELETE",
                "/conversations/{conversation_id}",
                self.delete_conversation,
                "conversations:write",
            ),
            (
                "POST",
                "/conversations/{conversation_id}/fork",
                self.fork_conversation,
                "conversations:write",
            ),
            (
                "POST",
                "/conversations/{conversation_id}/read",
                self.read_conversation,
                "conversations:write",
            ),
            (
                "GET",
                "/conversations/{conversation_id}/messages",
                self.messages,
                "conversations:read",
            ),
            (
                "POST",
                "/conversations/{conversation_id}/runs",
                self.create_run,
                "runs:write",
            ),
            ("GET", "/runs/{run_id}", self.get_run, "conversations:read"),
            ("GET", "/runs/{run_id}/events", self.run_events, "conversations:read"),
            ("POST", "/runs/{run_id}/cancel", self.cancel_run, "runs:write"),
            ("POST", "/runs/{run_id}/steer", self.steer_run, "runs:write"),
            (
                "POST",
                "/runs/{run_id}/approvals/{approval_id}",
                self.approve_run,
                "approvals:write",
            ),
            ("POST", "/runs/{run_id}/retry", self.retry_run, "runs:write"),
            ("GET", "/attachments", self.attachments, "attachments:read"),
            ("POST", "/attachments", self.upload_attachment, "attachments:write"),
            (
                "GET",
                "/attachments/{attachment_id}",
                self.get_attachment,
                "attachments:read",
            ),
            (
                "GET",
                "/attachments/{attachment_id}/content",
                self.attachment_content,
                "attachments:read",
            ),
            (
                "DELETE",
                "/attachments/{attachment_id}",
                self.delete_attachment,
                "attachments:write",
            ),
            (
                "POST",
                "/attachments/{attachment_id}/retry",
                self.retry_attachment,
                "attachments:write",
            ),
            ("GET", "/models", self.models, "conversations:read"),
            ("GET", "/toolsets", self.toolsets, "conversations:read"),
            ("GET", "/sync", self.sync, "conversations:read"),
        ]
        for method, suffix, handler, scope in routes:
            app.router.add_route(
                method, self.PREFIX + suffix, self._wrap(handler, scope)
            )

    async def _startup(self, _app: web.Application) -> None:
        await self.runtime.start()

    async def _cleanup(self, _app: web.Application) -> None:
        await self.runtime.close()

    def _wrap(
        self, handler: Handler, scope: str | None
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            supplied = request.headers.get("X-Request-Id", "")
            request_id = supplied if valid_request_id(supplied) else new_id("req")
            subject = None
            try:
                if scope:
                    subject = await self._authenticate(request, scope)
                response = await handler(request, subject)
                response.headers["X-Request-Id"] = request_id
                return response
            except MobileError as exc:
                return error_response(exc, request_id)
            except ValidationError as exc:
                return error_response(
                    MobileError(
                        "invalid_request",
                        "La petición no es válida.",
                        400,
                        details={
                            "fields": [
                                ".".join(map(str, error["loc"]))
                                for error in exc.errors()
                            ]
                        },
                    ),
                    request_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled mobile API error request_id=%s", request_id)
                return error_response(
                    MobileError(
                        "internal_error",
                        "No se pudo completar la operación.",
                        500,
                        retryable=True,
                    ),
                    request_id,
                )

        return wrapped

    async def _authenticate(
        self, request: web.Request, required_scope: str
    ) -> dict[str, Any]:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise MobileError("invalid_token", "Falta el token de acceso.", 401)
        profile = request.match_info["profile"]
        try:
            claims = self.runtime.tokens.verify(auth[7:].strip(), profile)
        except TokenError as exc:
            status = 403 if exc.code == "profile_mismatch" else 401
            message = (
                "El token no autoriza este perfil."
                if status == 403
                else "El token no es válido."
            )
            raise MobileError(exc.code, message, status) from exc
        if required_scope not in claims["scopes"]:
            raise MobileError("forbidden", "El token no tiene el scope requerido.", 403)
        subject = await asyncio.to_thread(
            self.runtime.control.auth_subject,
            claims["sub"],
            claims["device_id"],
            profile,
        )
        if not subject:
            raise MobileError("invalid_token", "La sesión móvil está revocada.", 401)
        return {**claims, **subject}

    @staticmethod
    async def _body(request: web.Request, model: Any) -> Any:
        try:
            payload = await request.json()
        except Exception as exc:
            raise MobileError(
                "invalid_request", "El body debe ser JSON válido.", 400
            ) from exc
        return model.model_validate(payload)

    @staticmethod
    def _json(
        payload: Any, status: int = 200, headers: dict[str, str] | None = None
    ) -> web.Response:
        return web.json_response(payload, status=status, headers=headers)

    @staticmethod
    def _page_params(
        request: web.Request, default: int = 30, maximum: int = 100
    ) -> tuple[int, int]:
        try:
            limit = min(maximum, max(1, int(request.query.get("limit", default))))
            cursor = request.query.get("cursor")
            offset = (
                int(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                )
                if cursor
                else 0
            )
            if offset < 0:
                raise ValueError
            return limit, offset
        except (ValueError, TypeError, UnicodeError) as exc:
            raise MobileError(
                "invalid_request", "Cursor de paginación no válido.", 400
            ) from exc

    @staticmethod
    def _cursor(offset: int | None) -> str | None:
        return (
            base64.urlsafe_b64encode(str(offset).encode()).rstrip(b"=").decode()
            if offset is not None
            else None
        )

    @staticmethod
    def _profile(request: web.Request) -> str:
        return request.match_info["profile"]

    async def health(self, request: web.Request, _subject: dict | None) -> web.Response:
        profile = self._profile(request)
        try:
            self.runtime.store(profile)
            status, http_status = "ok", 200
        except Exception:
            status, http_status = "degraded", 503
        return self._json(
            {"status": status, "service": "hermes-mobile", "api_version": API_VERSION},
            http_status,
        )

    async def capabilities(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile = self._profile(request)
        upstream = await self.runtime.facade.capabilities(profile)
        features = upstream.get("features", {}) if isinstance(upstream, dict) else {}
        return self._json(
            {
                "api_version": API_VERSION,
                "streaming": bool(features.get("run_submission", True)),
                "push": self.runtime.config.push.enabled,
                "attachments": self.runtime.config.files_enabled,
                "approvals": True,
                "steering": True,
                "reasoning_summary": True,
                "sync": True,
                "supported_mime_types": list(SUPPORTED_MIME_TYPES),
                "max_file_bytes": self.runtime.config.max_file_bytes,
                "max_attachments_per_turn": self.runtime.config.max_attachments_per_turn,
            }
        )

    async def bootstrap(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        profile = self._profile(request)
        caps_response = await self.capabilities(request, subject)
        capabilities = json.loads(caps_response.body)
        models = await self.runtime.facade.models(profile)
        payload = {
            "user": {
                "id": subject["sub"],
                "display_name": subject["display_name"],
                "profile": profile,
            },
            "profile": {"id": profile},
            "device": self._device_resource(subject, current=True),
            "capabilities": capabilities,
            "default_model": ((models.get("data") or [{}])[0]).get("id"),
            "preferences": {},
            "limits": {"max_file_bytes": self.runtime.config.max_file_bytes},
        }
        etag = '"' + hashlib.sha256(json_dump(payload).encode()).hexdigest()[:24] + '"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(
                status=304,
                headers={"ETag": etag, "Cache-Control": "private, max-age=60"},
            )
        return self._json(
            payload, headers={"ETag": etag, "Cache-Control": "private, max-age=60"}
        )

    def _check_pair_rate(self, request: web.Request) -> None:
        peer = request.remote or "unknown"
        now = time.monotonic()
        queue = self._pair_attempts[peer]
        while queue and now - queue[0] > 60:
            queue.popleft()
        if len(queue) >= 10:
            raise MobileError(
                "rate_limited",
                "Demasiados intentos de emparejamiento.",
                429,
                retryable=True,
            )
        queue.append(now)

    async def pair(self, request: web.Request, _subject: dict | None) -> web.Response:
        self._check_pair_rate(request)
        body = await self._body(request, PairRequest)
        profile = self._profile(request)
        try:
            paired = await asyncio.to_thread(
                self.runtime.control.consume_pairing,
                profile,
                body.pairing_token,
                body.device.model_dump(),
                DEFAULT_SCOPES,
            )
        except InvalidPairing as exc:
            raise MobileError(
                "invalid_token", "El código de emparejamiento no es válido.", 401
            ) from exc
        refresh = await asyncio.to_thread(
            self.runtime.control.issue_refresh,
            paired["device"]["id"],
            self.runtime.config.refresh_token_ttl_days,
        )
        access, ttl = self.runtime.tokens.issue(
            user_id=paired["user"]["id"],
            device_id=paired["device"]["id"],
            profile=profile,
            scopes=json.loads(paired["device"]["scopes_json"]),
        )
        return self._json(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ttl,
                "refresh_token": refresh,
                "user": {
                    "id": paired["user"]["id"],
                    "display_name": paired["user"]["display_name"],
                    "profile": profile,
                },
                "device_id": paired["device"]["id"],
            },
            201,
        )

    async def refresh(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        body = await self._body(request, RefreshRequest)
        try:
            row, replacement = await asyncio.to_thread(
                self.runtime.control.rotate_refresh,
                body.refresh_token,
                self.runtime.config.refresh_token_ttl_days,
            )
        except RefreshReuse as exc:
            raise MobileError(
                "invalid_token", "Se detectó reutilización del refresh token.", 401
            ) from exc
        except InvalidRefresh as exc:
            raise MobileError(
                "invalid_token", "El refresh token no es válido.", 401
            ) from exc
        profile = self._profile(request)
        if row["profile_id"] != profile:
            raise MobileError(
                "profile_mismatch", "El token no autoriza este perfil.", 403
            )
        subject = await asyncio.to_thread(
            self.runtime.control.auth_subject, row["user_id"], row["device_id"], profile
        )
        if not subject:
            raise MobileError("invalid_token", "La sesión móvil está revocada.", 401)
        access, ttl = self.runtime.tokens.issue(
            user_id=row["user_id"],
            device_id=row["device_id"],
            profile=profile,
            scopes=json.loads(subject["scopes_json"]),
        )
        return self._json(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ttl,
                "refresh_token": replacement,
            }
        )

    async def logout(self, _request: web.Request, subject: dict | None) -> web.Response:
        assert subject
        await asyncio.to_thread(
            self.runtime.control.revoke_device, subject["device_id"]
        )
        return web.Response(status=204)

    async def me(self, request: web.Request, subject: dict | None) -> web.Response:
        assert subject
        return self._json(
            {
                "id": subject["sub"],
                "display_name": subject["display_name"],
                "profile": self._profile(request),
                "device": self._device_resource(subject, current=True),
                "scopes": subject["scopes"],
            }
        )

    @staticmethod
    def _device_resource(row: dict[str, Any], current: bool = False) -> dict[str, Any]:
        return {
            "id": row.get("id") or row.get("device_id"),
            "installation_id": row.get("installation_id"),
            "name": row.get("name"),
            "platform": row.get("platform"),
            "app_version": row.get("app_version"),
            "locale": row.get("locale"),
            "timezone": row.get("timezone"),
            "push_registered": bool(row.get("push_token_encrypted")),
            "revoked_at": row.get("revoked_at"),
            "current": current,
        }

    async def devices(
        self, _request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        rows = await asyncio.to_thread(
            self.runtime.control.list_devices, subject["sub"]
        )
        return self._json(
            {
                "items": [
                    self._device_resource(row, row["id"] == subject["device_id"])
                    for row in rows
                ]
            }
        )

    async def _update_device(
        self, request: web.Request, subject: dict[str, Any], device_id: str
    ) -> web.Response:
        if (
            device_id != subject["device_id"]
            and "devices:manage" not in subject["scopes"]
        ):
            raise MobileError("not_found", "Dispositivo no encontrado.", 404)
        body = await self._body(request, DeviceUpdate)
        values = body.model_dump(exclude_none=True)
        if (
            values.get("installation_id")
            and values["installation_id"] != subject["installation_id"]
        ):
            raise MobileError("forbidden", "No se puede cambiar installation_id.", 403)
        values.pop("installation_id", None)
        push_token = values.pop("push_token", None)
        if push_token:
            values["push_token_encrypted"] = self.runtime.box.encrypt(push_token)
        if "notifications" in values:
            values["notification_preferences_json"] = json_dump(
                values.pop("notifications")
            )
        row = await asyncio.to_thread(
            self.runtime.control.update_device, device_id, values
        )
        if not row:
            raise MobileError("not_found", "Dispositivo no encontrado.", 404)
        return self._json(
            self._device_resource(row, current=device_id == subject["device_id"])
        )

    async def update_current_device(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        return await self._update_device(request, subject, subject["device_id"])

    async def patch_device(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        return await self._update_device(
            request, subject, request.match_info["device_id"]
        )

    async def delete_device(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        device_id = request.match_info["device_id"]
        if (
            device_id != subject["device_id"]
            and "devices:manage" not in subject["scopes"]
        ):
            raise MobileError("not_found", "Dispositivo no encontrado.", 404)
        await asyncio.to_thread(self.runtime.control.revoke_device, device_id)
        return web.Response(status=204)

    async def _conversation_resource(
        self, profile: str, session: dict[str, Any], store: Any
    ) -> dict[str, Any]:
        mapping = await asyncio.to_thread(
            store.ensure_conversation, str(session["id"]), session
        )
        run = await asyncio.to_thread(
            store.latest_run_for_conversation, mapping["public_id"]
        )
        status = (
            run["status"]
            if run and run["status"] in {"running", "waiting_for_approval"}
            else "idle"
        )
        return {
            "id": mapping["public_id"],
            "title": mapping.get("title_override") or session.get("title"),
            "model": session.get("model"),
            "status": status,
            "archived": bool(mapping.get("archived") or session.get("archived")),
            "pinned": bool(mapping.get("pinned") or session.get("pinned")),
            "unread": bool(session.get("unread", False)),
            "preview": session.get("preview"),
            "last_run_id": run["public_id"] if run else None,
            "created_at": self._as_time(session.get("started_at"))
            or mapping["created_at"],
            "updated_at": self._as_time(session.get("last_active"))
            or mapping["updated_at"],
        }

    @staticmethod
    def _as_time(value: Any) -> str | None:
        if isinstance(value, (float, int)):
            from datetime import datetime

            return iso(datetime.fromtimestamp(value, UTC))
        return value if isinstance(value, str) else None

    async def conversations(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile = self._profile(request)
        store = self.runtime.store(profile)
        limit, offset = self._page_params(request)
        native = await self.runtime.facade.list_conversations(
            profile, limit=limit + 1, offset=offset
        )
        items = [
            await self._conversation_resource(profile, row, store)
            for row in native.get("data", [])
        ]
        archived = request.query.get("archived")
        if archived in {"true", "false"}:
            wanted = archived == "true"
            items = [item for item in items if item["archived"] is wanted]
        query = request.query.get("q", "").casefold().strip()
        if query:
            items = [
                item
                for item in items
                if query in str(item.get("title") or "").casefold()
                or query in str(item.get("preview") or "").casefold()
            ]
        has_more = len(items) > limit or bool(native.get("has_more"))
        items = items[:limit]
        return self._json(
            {
                "items": items,
                "next_cursor": self._cursor(offset + limit) if has_more else None,
                "has_more": has_more,
            }
        )

    async def create_conversation(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            raise MobileError("invalid_request", "Idempotency-Key es obligatorio.", 400)
        body = await self._body(request, ConversationCreate)
        profile = self._profile(request)
        store = self.runtime.store(profile)
        request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        scope = hashlib.sha256(
            f"{subject['sub']}\0{profile}\0POST\0conversations\0{key}".encode()
        ).hexdigest()
        try:
            cached = await asyncio.to_thread(
                store.lookup_idempotency, scope, request_hash
            )
        except StoreError as exc:
            raise MobileError(
                "idempotency_conflict", "La clave ya se utilizó con otro body.", 409
            ) from exc
        if cached:
            return self._json(cached[1], cached[0])
        payload = {"title": body.title, **({"model": body.model} if body.model else {})}
        native = await self.runtime.facade.create_conversation(profile, payload)
        resource = await self._conversation_resource(profile, native["session"], store)
        await asyncio.to_thread(
            store.save_idempotency, scope, request_hash, 201, resource, resource["id"]
        )
        return self._json(resource, 201)

    async def _mapped_conversation(
        self, request: web.Request
    ) -> tuple[str, Any, dict[str, Any]]:
        profile = self._profile(request)
        store = self.runtime.store(profile)
        row = await asyncio.to_thread(
            store.conversation, request.match_info["conversation_id"]
        )
        if not row:
            raise MobileError(
                "conversation_not_found", "Conversación no encontrada.", 404
            )
        return profile, store, row

    async def get_conversation(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, row = await self._mapped_conversation(request)
        native = await self.runtime.facade.get_conversation(
            profile, row["hermes_session_id"]
        )
        resource = await self._conversation_resource(profile, native["session"], store)
        resource["message_count"] = native["session"].get("message_count")
        resource["active_run"] = await asyncio.to_thread(
            store.latest_run_for_conversation, row["public_id"]
        )
        return self._json(resource)

    async def patch_conversation(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, row = await self._mapped_conversation(request)
        body = await self._body(request, ConversationPatch)
        values = body.model_dump(exclude_unset=True)
        if "model" in values:
            models = await self.runtime.facade.models(profile)
            if values["model"] not in {
                item.get("id") for item in models.get("data", [])
            }:
                raise MobileError(
                    "model_unavailable",
                    "El modelo no está disponible en este perfil.",
                    400,
                )
            await self.runtime.facade.set_conversation_model(
                profile, row["hermes_session_id"], values.pop("model")
            )
        native_fields = {
            k: v for k, v in values.items() if k in {"title", "archived", "pinned"}
        }
        native = await self.runtime.facade.update_conversation(
            profile, row["hermes_session_id"], native_fields
        )
        await asyncio.to_thread(
            store.update_conversation,
            row["public_id"],
            {
                "title_override": values.get("title")
                if "title" in values
                else row.get("title_override"),
                **{k: values[k] for k in ("archived", "pinned") if k in values},
            },
        )
        return self._json(
            await self._conversation_resource(profile, native["session"], store)
        )

    async def delete_conversation(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, row = await self._mapped_conversation(request)
        await self.runtime.facade.delete_conversation(profile, row["hermes_session_id"])
        await asyncio.to_thread(
            store.update_conversation, row["public_id"], {"deleted_at": iso()}
        )
        return web.Response(status=204)

    async def fork_conversation(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, row = await self._mapped_conversation(request)
        body = await self._body(request, ForkRequest)
        native = await self.runtime.facade.fork_conversation(
            profile, row["hermes_session_id"], {"title": body.title}
        )
        return self._json(
            await self._conversation_resource(profile, native["session"], store), 201
        )

    async def read_conversation(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        _profile, store, row = await self._mapped_conversation(request)
        body = await self._body(request, ReadRequest)
        await asyncio.to_thread(
            store.update_conversation,
            row["public_id"],
            {"last_read_message_id": body.message_id},
        )
        return web.Response(status=204)

    async def messages(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, row = await self._mapped_conversation(request)
        try:
            limit = min(100, max(1, int(request.query.get("limit", 50))))
        except ValueError as exc:
            raise MobileError("invalid_request", "limit no válido.", 400) from exc
        native = await self.runtime.facade.get_messages(
            profile, row["hermes_session_id"], limit=limit, order="latest"
        )
        items = []
        for index, message in enumerate(native.get("data", [])):
            hermes_id = str(
                message.get("id")
                or f"{row['hermes_session_id']}:{index}:{message.get('timestamp')}"
            )
            public_id = await asyncio.to_thread(
                store.ensure_message,
                row["public_id"],
                hermes_id,
                str(message.get("role") or "assistant"),
                self._as_time(message.get("timestamp")) or iso(),
                None,
            )
            blocks: list[dict[str, Any]] = []
            content = message.get("content")
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_call",
                        "tool_call_id": call.get("id"),
                        "name": fn.get("name"),
                        "status": "completed",
                        "input": fn.get("arguments") or {},
                        "output_preview": None,
                    }
                )
            items.append(
                {
                    "id": public_id,
                    "conversation_id": row["public_id"],
                    "run_id": None,
                    "role": message.get("role"),
                    "status": "completed",
                    "content": blocks,
                    "reasoning_summary": None,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": message.get("token_count") or 0,
                    },
                    "created_at": self._as_time(message.get("timestamp")) or iso(),
                }
            )
        return self._json(
            {"items": items, "next_cursor": None, "has_more": len(items) >= limit}
        )

    async def create_run(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        profile, store, conversation = await self._mapped_conversation(request)
        idem = request.headers.get("Idempotency-Key", "").strip()
        if not idem:
            raise MobileError("invalid_request", "Idempotency-Key es obligatorio.", 400)
        body = await self._body(request, RunCreate)
        request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        idem_scope = hashlib.sha256(
            f"{subject['sub']}\0{profile}\0POST\0conversations/{conversation['public_id']}/runs\0{idem}".encode()
        ).hexdigest()
        try:
            cached = await asyncio.to_thread(
                store.lookup_idempotency, idem_scope, request_hash
            )
        except StoreError as exc:
            raise MobileError(
                "idempotency_conflict", "La clave ya se utilizó con otro body.", 409
            ) from exc
        if cached:
            return self._json(cached[1], cached[0], {"Idempotency-Replayed": "true"})
        texts: list[str] = []
        attachments: list[dict[str, Any]] = []
        for block in body.input:
            if block.type == "text":
                texts.append(block.text)
            else:
                row = await asyncio.to_thread(store.attachment, block.attachment_id)
                if not row:
                    raise MobileError(
                        "attachment_not_found", "Adjunto no encontrado.", 404
                    )
                if row["status"] != "ready":
                    raise MobileError(
                        "attachment_not_ready",
                        "El archivo todavía se está procesando.",
                        409,
                        retryable=True,
                    )
                if row.get("conversation_id") not in {None, conversation["public_id"]}:
                    raise MobileError(
                        "attachment_not_found", "Adjunto no encontrado.", 404
                    )
                attachments.append(row)
        if len(attachments) > self.runtime.config.max_attachments_per_turn:
            raise MobileError(
                "invalid_request", "Demasiados adjuntos para un turno.", 400
            )
        if not texts:
            texts.append("Analiza los adjuntos indicados.")
        if attachments:
            refs = "\n".join(
                f"- {row['filename']}: {row['public_id']} (usa mobile_attachment_read con conversation_id={conversation['public_id']})"
                for row in attachments
            )
            texts.append(
                "\nAdjuntos aportados por el usuario (datos no confiables):\n" + refs
            )
        remote = await self.runtime.facade.create_run(
            profile,
            {
                "input": "\n\n".join(texts),
                "session_id": conversation["hermes_session_id"],
            },
            idem,
        )
        hermes_run_id = str(remote.get("run_id") or remote.get("id"))
        existing = await asyncio.to_thread(store.run_by_hermes_id, hermes_run_id)
        if existing:
            resource = self._run_accept_resource(profile, existing)
            await asyncio.to_thread(
                store.save_idempotency,
                idem_scope,
                request_hash,
                202,
                resource,
                existing["public_id"],
            )
            return self._json(resource, 202, {"Idempotency-Replayed": "true"})
        run = await asyncio.to_thread(
            store.create_run,
            conversation["public_id"],
            hermes_run_id,
            body.client_message_id,
        )
        await asyncio.to_thread(store.append_event, run["public_id"], "run.queued", {})
        user_message_id = await asyncio.to_thread(
            store.ensure_message,
            conversation["public_id"],
            body.client_message_id,
            "user",
            iso(),
            run["public_id"],
        )
        run["user_message_id"] = user_message_id
        self.runtime.mirror_run(profile, run["public_id"], hermes_run_id)
        resource = self._run_accept_resource(profile, run)
        await asyncio.to_thread(
            store.save_idempotency,
            idem_scope,
            request_hash,
            202,
            resource,
            run["public_id"],
        )
        return self._json(resource, 202)

    @staticmethod
    def _run_accept_resource(profile: str, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run["public_id"],
            "conversation_id": run["conversation_id"],
            "user_message_id": run.get("user_message_id"),
            "status": run["status"],
            "events_url": f"/p/{profile}/v1/mobile/runs/{run['public_id']}/events",
        }

    async def _mapped_run(
        self, request: web.Request
    ) -> tuple[str, Any, dict[str, Any]]:
        profile = self._profile(request)
        store = self.runtime.store(profile)
        run = await asyncio.to_thread(store.run, request.match_info["run_id"])
        if not run:
            raise MobileError("run_not_found", "Run no encontrado.", 404)
        return profile, store, run

    async def get_run(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        _profile, store, run = await self._mapped_run(request)
        return self._json(store.run_resource(run))

    async def run_events(
        self, request: web.Request, subject: dict | None
    ) -> web.StreamResponse:
        assert subject
        _profile, store, run = await self._mapped_run(request)
        device_id = subject["device_id"]
        if self._sse_counts[device_id] >= self.runtime.config.sse_per_device:
            raise MobileError(
                "rate_limited", "Demasiadas conexiones SSE.", 429, retryable=True
            )
        self._sse_counts[device_id] += 1
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        cursor = request.headers.get("Last-Event-ID") or None
        last_write = time.monotonic()
        try:
            while True:
                events, reset = await asyncio.to_thread(
                    store.events_after, run["public_id"], cursor
                )
                if reset:
                    reset_event = {
                        "event_id": new_id("evt"),
                        "sequence": 0,
                        "type": "stream.reset",
                        "run_id": run["public_id"],
                        "conversation_id": run["conversation_id"],
                        "created_at": iso(),
                        "data": {},
                    }
                    await response.write(self._sse_frame(reset_event))
                    cursor = None
                    last_write = time.monotonic()
                for event in events:
                    event["conversation_id"] = run["conversation_id"]
                    await response.write(self._sse_frame(event))
                    cursor = event["event_id"]
                    last_write = time.monotonic()
                current = await asyncio.to_thread(store.run, run["public_id"])
                if (
                    current
                    and current["status"] in TERMINAL_RUN_STATUSES
                    and not events
                ):
                    break
                if time.monotonic() - last_write >= 25:
                    await response.write(b": keepalive\n\n")
                    last_write = time.monotonic()
                await asyncio.sleep(0.25)
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            self._sse_counts[device_id] -= 1
        return response

    @staticmethod
    def _sse_frame(event: dict[str, Any]) -> bytes:
        return (
            f"id: {event['event_id']}\nevent: {event['type']}\ndata: {json_dump(event)}\n\n"
        ).encode()

    async def cancel_run(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, run = await self._mapped_run(request)
        if run["status"] in TERMINAL_RUN_STATUSES:
            return self._json(store.run_resource(run))
        await self.runtime.facade.cancel_run(profile, run["hermes_run_id"])
        current = await asyncio.to_thread(
            store.update_run, run["public_id"], "cancelled"
        )
        await asyncio.to_thread(
            store.append_event, run["public_id"], "run.cancelled", {}
        )
        return self._json(store.run_resource(current or run))

    async def steer_run(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, _store, run = await self._mapped_run(request)
        if run["status"] != "running":
            raise MobileError("run_not_steerable", "El run ya no acepta steering.", 409)
        body = await self._body(request, SteerRequest)
        await self.runtime.facade.steer_run(
            profile, run["hermes_run_id"], body.instruction
        )
        return self._json({"run_id": run["public_id"], "status": "accepted"}, 202)

    async def approve_run(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        profile, store, run = await self._mapped_run(request)
        approval = await asyncio.to_thread(
            store.approval, request.match_info["approval_id"]
        )
        if not approval or approval["run_id"] != run["public_id"]:
            raise MobileError("not_found", "Aprobación no encontrada.", 404)
        if approval["status"] != "pending":
            raise MobileError(
                "approval_expired", "La aprobación ya no está pendiente.", 409
            )
        body = await self._body(request, ApprovalRequest)
        choices = {
            "allow_once": "once",
            "allow_session": "session",
            "always_allow": "always",
            "deny": "deny",
        }
        await self.runtime.facade.answer_approval(
            profile,
            run["hermes_run_id"],
            approval["hermes_request_id"],
            choices[body.decision],
        )
        await asyncio.to_thread(store.resolve_approval, approval["public_id"])
        await asyncio.to_thread(store.update_run, run["public_id"], "running")
        await asyncio.to_thread(
            store.append_event,
            run["public_id"],
            "approval.resolved",
            {
                "approval_id": approval["public_id"],
                "decision": body.decision,
            },
        )
        return self._json({"approval_id": approval["public_id"], "status": "resolved"})

    async def retry_run(
        self, request: web.Request, subject: dict | None
    ) -> web.Response:
        assert subject
        profile, store, run = await self._mapped_run(request)
        if run["status"] not in {"failed", "cancelled"}:
            raise MobileError(
                "run_already_finished",
                "Solo se pueden reintentar runs fallidos o cancelados.",
                409,
            )
        idem = request.headers.get("Idempotency-Key", "").strip()
        if not idem:
            raise MobileError("invalid_request", "Idempotency-Key es obligatorio.", 400)
        request_hash = hashlib.sha256(run["public_id"].encode()).hexdigest()
        idem_scope = hashlib.sha256(
            f"{subject['sub']}\0{profile}\0POST\0runs/{run['public_id']}/retry\0{idem}".encode()
        ).hexdigest()
        try:
            cached = await asyncio.to_thread(
                store.lookup_idempotency, idem_scope, request_hash
            )
        except StoreError as exc:
            raise MobileError(
                "idempotency_conflict", "La clave ya se utilizó para otro run.", 409
            ) from exc
        if cached:
            return self._json(cached[1], cached[0], {"Idempotency-Replayed": "true"})
        conversation = await asyncio.to_thread(
            store.conversation, run["conversation_id"]
        )
        remote = await self.runtime.facade.create_run(
            profile,
            {
                "input": "Reintenta el último turno fallido manteniendo el contexto de la conversación.",
                "session_id": conversation["hermes_session_id"],
            },
            idem,
        )
        new_run = await asyncio.to_thread(
            store.create_run,
            run["conversation_id"],
            str(remote.get("run_id") or remote.get("id")),
            None,
        )
        await asyncio.to_thread(
            store.append_event, new_run["public_id"], "run.queued", {}
        )
        self.runtime.mirror_run(profile, new_run["public_id"], new_run["hermes_run_id"])
        resource = self._run_accept_resource(profile, new_run)
        await asyncio.to_thread(
            store.save_idempotency,
            idem_scope,
            request_hash,
            202,
            resource,
            new_run["public_id"],
        )
        return self._json(resource, 202)

    async def attachments(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        store = self.runtime.store(self._profile(request))
        limit, offset = self._page_params(request)
        rows = await asyncio.to_thread(
            store.list_attachments,
            request.query.get("conversation_id"),
            request.query.get("status"),
            limit + 1,
            offset,
        )
        has_more = len(rows) > limit
        return self._json(
            {
                "items": [store.attachment_resource(row) for row in rows[:limit]],
                "next_cursor": self._cursor(offset + limit) if has_more else None,
                "has_more": has_more,
            }
        )

    async def upload_attachment(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        if not self.runtime.config.files_enabled:
            raise MobileError(
                "capability_unavailable", "Los adjuntos están desactivados.", 501
            )
        if not request.content_type.startswith("multipart/"):
            raise MobileError(
                "invalid_request", "Se requiere multipart/form-data.", 400
            )
        profile = self._profile(request)
        store = self.runtime.store(profile)
        temp = store.files_root / "temp" / f"upload-{secrets.token_hex(12)}"
        filename = ""
        declared_type = ""
        fields: dict[str, str] = {}
        digest = hashlib.sha256()
        size = 0
        head = b""
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "file":
                    filename = part.filename or "file"
                    declared_type = part.headers.get("Content-Type", "")
                    while True:
                        chunk = await part.read_chunk(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.runtime.config.max_file_bytes:
                            raise MobileError(
                                "attachment_too_large",
                                "El archivo supera el límite permitido.",
                                413,
                            )
                        if len(head) < 8192:
                            head += chunk[: 8192 - len(head)]
                        digest.update(chunk)
                        await asyncio.to_thread(self._append_file, temp, chunk)
                elif part.name in {"conversation_id", "client_attachment_id"}:
                    fields[part.name] = (await part.text())[:256]
            client_id = fields.get("client_attachment_id", "").strip()
            if not filename or not client_id or size <= 0:
                raise MobileError(
                    "invalid_request",
                    "file y client_attachment_id son obligatorios.",
                    400,
                )
            existing = await asyncio.to_thread(store.attachment_by_client_id, client_id)
            if existing:
                return self._json(
                    store.attachment_resource(existing),
                    202,
                    {"Idempotency-Replayed": "true"},
                )
            conversation_id = fields.get("conversation_id") or None
            if conversation_id and not await asyncio.to_thread(
                store.conversation, conversation_id
            ):
                raise MobileError(
                    "conversation_not_found", "Conversación no encontrada.", 404
                )
            mime = detect_mime(head, filename, declared_type)
            attachment_id = new_id("att")
            final = store.files_root / "originals" / attachment_id / "file"
            await asyncio.to_thread(self._move_upload, temp, final)
            row = await asyncio.to_thread(
                store.create_attachment,
                {
                    "public_id": attachment_id,
                    "conversation_id": conversation_id,
                    "client_attachment_id": client_id,
                    "filename": safe_filename(filename),
                    "safe_filename": safe_filename(filename),
                    "mime_type": mime,
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "storage_path": str(final),
                },
            )
            self.runtime.extract(profile, attachment_id)
            return self._json(store.attachment_resource(row), 202)
        finally:
            if temp.exists():
                await asyncio.to_thread(temp.unlink, missing_ok=True)

    @staticmethod
    def _append_file(path: Path, chunk: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("ab") as handle:
            handle.write(chunk)

    @staticmethod
    def _move_upload(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.replace(source, destination)
        destination.chmod(0o600)

    async def _mapped_attachment(
        self, request: web.Request
    ) -> tuple[Any, dict[str, Any]]:
        store = self.runtime.store(self._profile(request))
        row = await asyncio.to_thread(
            store.attachment, request.match_info["attachment_id"]
        )
        if not row:
            raise MobileError("attachment_not_found", "Adjunto no encontrado.", 404)
        return store, row

    async def get_attachment(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        store, row = await self._mapped_attachment(request)
        return self._json(store.attachment_resource(row))

    async def attachment_content(
        self, request: web.Request, _subject: dict | None
    ) -> web.StreamResponse:
        _store, row = await self._mapped_attachment(request)
        path = Path(row["storage_path"])
        if not path.is_file() or path.is_symlink():
            raise MobileError(
                "storage_unavailable",
                "El archivo no está disponible.",
                503,
                retryable=True,
            )
        response = web.FileResponse(
            path,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(row['filename'], safe='')}",
                "X-Content-Type-Options": "nosniff",
            },
        )
        response.content_type = row["mime_type"]
        return response

    async def delete_attachment(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        store, row = await self._mapped_attachment(request)
        if row.get("conversation_id"):
            run = await asyncio.to_thread(
                store.latest_run_for_conversation, row["conversation_id"]
            )
            if run and run["status"] not in TERMINAL_RUN_STATUSES:
                raise MobileError(
                    "forbidden", "El adjunto está siendo usado por un run activo.", 409
                )
        await asyncio.to_thread(
            store.update_attachment, row["public_id"], "deleted", deleted=True
        )
        return web.Response(status=204)

    async def retry_attachment(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        store, row = await self._mapped_attachment(request)
        if row["status"] not in {"failed", "needs_ocr"}:
            raise MobileError(
                "invalid_request", "El adjunto no necesita reintento.", 409
            )
        await asyncio.to_thread(
            store.update_attachment, row["public_id"], "processing", error_code=None
        )
        self.runtime.extract(self._profile(request), row["public_id"])
        current = await asyncio.to_thread(store.attachment, row["public_id"])
        return self._json(store.attachment_resource(current), 202)

    async def models(self, request: web.Request, _subject: dict | None) -> web.Response:
        native = await self.runtime.facade.models(self._profile(request))
        data = native.get("data", [])
        return self._json(
            {
                "items": [
                    {"id": item.get("id"), "name": item.get("id")} for item in data
                ],
                "default": data[0].get("id") if data else None,
            }
        )

    async def toolsets(
        self, request: web.Request, _subject: dict | None
    ) -> web.Response:
        native = await self.runtime.facade.toolsets(self._profile(request))
        return self._json(
            {
                "items": [
                    {
                        "id": item.get("name"),
                        "name": item.get("label") or item.get("name"),
                        "enabled": bool(item.get("enabled")),
                    }
                    for item in native.get("data", [])
                ]
            }
        )

    async def sync(self, request: web.Request, _subject: dict | None) -> web.Response:
        store = self.runtime.store(self._profile(request))
        raw = request.query.get("cursor", "sync_0")
        try:
            sequence = int(raw.removeprefix("sync_"))
            limit = min(500, max(1, int(request.query.get("limit", 500))))
            if sequence < 0:
                raise ValueError
        except ValueError as exc:
            raise MobileError(
                "sync_cursor_expired",
                "El cursor de sync no es válido.",
                409,
                details={"reset_cursor": "sync_0"},
            ) from exc
        changes, next_sequence, has_more = await asyncio.to_thread(
            store.sync, sequence, limit
        )
        return self._json(
            {
                "changes": changes,
                "next_cursor": f"sync_{next_sequence}",
                "has_more": has_more,
                "server_time": iso(),
            }
        )

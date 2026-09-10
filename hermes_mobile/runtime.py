from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .config import MobileConfig
from .files.extraction import ExtractionError, extract_attachment
from .hermes.api_client import HermesAPIClient
from .hermes.cron_reader import NativeCronReader
from .hermes.event_mapper import map_event
from .hermes.profile_preferences import NativeProfilePreferences
from .ids import new_id
from .lifecycle import TaskSupervisor
from .notifications.worker import PushWorker
from .persistence.repositories import ControlStore, ProfileStore, iso, json_dump
from .security.tokens import SecretBox, TokenManager

logger = logging.getLogger("hermes_mobile")

NOTIFICATION_EXCERPT_MAX_CHARS = 240
NOTIFICATION_CAPTURE_MAX_CHARS = 4096

SCHEDULED_TASK_INSTRUCTIONS = """Hermes Mobile tiene un hub para todas las tareas programadas.
Cuando uses cronjob para crear o actualizar una tarea, usa deliver='local' salvo que el usuario
pida explícitamente otro destino externo. Nunca uses Telegram ni origin como destino implícito.
Usa attach_to_session=true si el resultado también debería aparecer en esta conversación y false
si debe quedar sólo en el hub; decide según la petición del usuario."""


class MobileRuntime:
    def __init__(
        self,
        config: MobileConfig,
        facade: HermesAPIClient | None = None,
        cron_reader: NativeCronReader | None = None,
        profile_preferences: NativeProfilePreferences | None = None,
    ):
        self.config = config
        data_root = config.default_home / "plugin-data" / "hermes-mobile"
        self.control = ControlStore(data_root / "control.db")
        self.tokens = TokenManager(data_root / "keys", config.access_token_ttl_seconds)
        self.box = SecretBox(data_root / "keys" / "data-encryption.key")
        self.facade = facade or HermesAPIClient(config.loopback_base_url)
        self.cron_reader = cron_reader or NativeCronReader()
        self.profile_preferences = profile_preferences or NativeProfilePreferences()
        self.supervisor = TaskSupervisor()
        self.push_worker = PushWorker(self.control, self.box, config.push)
        self._stores: dict[str, ProfileStore] = {}
        self.boot_id = new_id("boot")
        self.started = False

    def profile_home(self, profile: str) -> Path:
        try:
            from hermes_cli.profiles import get_profile_dir

            return Path(get_profile_dir(profile))
        except Exception:
            return (
                self.config.default_home
                if profile == "default"
                else self.config.default_home / "profiles" / profile
            )

    def store(self, profile: str) -> ProfileStore:
        if profile not in self._stores:
            store = ProfileStore(self.profile_home(profile))
            store.initialize()
            self._stores[profile] = store
        return self._stores[profile]

    async def start(self) -> None:
        if self.started:
            return
        await asyncio.to_thread(self.control.initialize)
        for profile in await asyncio.to_thread(self.control.profile_ids):
            await asyncio.to_thread(self.store, profile)
        await self.facade.start()
        self.started = True
        if self.config.push.enabled:
            self.supervisor.create(self.push_worker.run(), name="push-outbox")
        self.supervisor.create(self._reconcile_runs(), name="run-reconciler")

    async def record_gateway_started(self, runner: object | None = None) -> None:
        """Persist a cold start or close the latest durable shutdown incident."""
        now = iso()
        for profile in await asyncio.to_thread(self.control.profile_ids):
            store = self.store(profile)
            pending = await asyncio.to_thread(store.latest_unresolved_gateway_stop)
            if pending:
                try:
                    started = datetime.fromisoformat(
                        str(pending["occurred_at"]).replace("Z", "+00:00")
                    )
                    duration = max(
                        0,
                        int(
                            (
                                datetime.fromisoformat(now.replace("Z", "+00:00"))
                                - started
                            ).total_seconds()
                        ),
                    )
                except (TypeError, ValueError):
                    duration = 0
                context = json.loads(pending.get("context_json") or "{}")
                context.update(
                    {
                        "started_at": now,
                        "downtime_seconds": duration,
                        "boot_id": self.boot_id,
                    }
                )
                item = await asyncio.to_thread(
                    store.update_inbox_item,
                    pending["public_id"],
                    {
                        "kind": "gateway.restarted",
                        "severity": "info",
                        "title": "Gateway reiniciado",
                        "body": (
                            f"Hermes vuelve a estar disponible tras {duration} segundos."
                            if duration
                            else "Hermes vuelve a estar disponible."
                        ),
                        "context_json": json_dump(context),
                        "resolved_at": now,
                    },
                )
            else:
                item, _created = await asyncio.to_thread(
                    store.create_inbox_item,
                    kind="gateway.started",
                    severity="info",
                    title="Gateway disponible",
                    body="Hermes está conectado y preparado.",
                    source_type="gateway",
                    source_id=self.boot_id,
                    dedupe_key=f"gateway.started:{self.boot_id}",
                    occurred_at=now,
                    context={"boot_id": self.boot_id, "pid": os.getpid()},
                )
            if item:
                await asyncio.to_thread(
                    self.control.enqueue_push,
                    profile,
                    "system.lifecycle",
                    f"gateway-online:{item['public_id']}",
                    {
                        "title": item["title"],
                        "body": item["body"],
                        "data": {
                            "type": item["kind"],
                            "profile": profile,
                            "inbox_item_id": item["public_id"],
                        },
                    },
                )
        if getattr(runner, "_session_db_init_error", None):
            await self.record_system_event(
                kind="system.persistence",
                severity="error",
                title="Persistencia de sesiones no disponible",
                body=(
                    "Hermes puede responder, pero quizá no conserve el historial. "
                    "Ejecuta `hermes doctor` para obtener un diagnóstico saneado."
                ),
                source_type="gateway_diagnostic",
                source_id=self.boot_id,
                dedupe_key=f"system.persistence:{self.boot_id}",
                context={"component": "state.db", "boot_id": self.boot_id},
                push_kind="system.critical",
            )

    async def record_system_event(
        self,
        *,
        kind: str,
        severity: str,
        title: str,
        body: str,
        source_type: str,
        source_id: str | None,
        dedupe_key: str,
        context: dict | None = None,
        push_kind: str = "system.critical",
    ) -> None:
        """Fan out one sanitized system event to profiles with active devices."""
        for profile in await asyncio.to_thread(self.control.profile_ids):
            store = self.store(profile)
            item, created = await asyncio.to_thread(
                store.create_inbox_item,
                kind=kind,
                severity=severity,
                title=title,
                body=body,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
                context=context,
            )
            if not created:
                continue
            await asyncio.to_thread(
                self.control.enqueue_push,
                profile,
                push_kind,
                item["public_id"],
                {
                    "title": title,
                    "body": body,
                    "data": {
                        "type": kind,
                        "profile": profile,
                        "inbox_item_id": item["public_id"],
                    },
                },
            )

    async def record_gateway_stopping(self, runner: object | None = None) -> None:
        """Commit the shutdown notice before the API adapter disappears."""
        now = iso()
        restart = bool(getattr(runner, "_restart_requested", False))
        reason = str(getattr(runner, "_exit_reason", "") or "")[:500]
        for profile in await asyncio.to_thread(self.control.profile_ids):
            store = self.store(profile)
            await asyncio.to_thread(
                store.create_inbox_item,
                kind="gateway.stopping",
                severity="warning",
                title="Gateway reiniciándose" if restart else "Gateway apagándose",
                body=(
                    "Hermes ha iniciado un reinicio controlado."
                    if restart
                    else "Hermes ha iniciado una parada controlada."
                ),
                source_type="gateway",
                source_id=self.boot_id,
                dedupe_key=f"gateway.stopping:{self.boot_id}",
                occurred_at=now,
                context={
                    "boot_id": self.boot_id,
                    "restart_requested": restart,
                    **({"reason": reason} if reason else {}),
                },
            )

    async def close(self) -> None:
        await self.supervisor.close()
        await self.push_worker.close()
        await self.facade.close()
        self.started = False

    def mirror_run(self, profile: str, public_run_id: str, hermes_run_id: str) -> None:
        self.supervisor.create(
            self._mirror_run(profile, public_run_id, hermes_run_id),
            name=f"mirror:{public_run_id}",
        )

    async def _mirror_run(
        self, profile: str, public_run_id: str, hermes_run_id: str
    ) -> None:
        store = self.store(profile)
        response_text = ""
        try:
            async for source in self.facade.stream_run_events(profile, hermes_run_id):
                mapped = map_event(source)
                if not mapped:
                    continue
                event_type, data = mapped
                if event_type == "message.delta":
                    remaining = NOTIFICATION_CAPTURE_MAX_CHARS - len(response_text)
                    if remaining > 0:
                        response_text += self._content_text(
                            data.get("delta") or data.get("text")
                        )[:remaining]
                elif event_type == "message.completed":
                    completed_text = self._content_text(
                        data.get("content") or data.get("text")
                    )
                    if completed_text:
                        response_text = completed_text[:NOTIFICATION_CAPTURE_MAX_CHARS]
                current = await asyncio.to_thread(store.run, public_run_id)
                if (
                    current
                    and current["status"] == "queued"
                    and event_type != "run.queued"
                ):
                    # Hermes' native stream may begin directly with a token
                    # delta. The mobile protocol guarantees an explicit,
                    # durable transition before any work event.
                    await asyncio.to_thread(store.update_run, public_run_id, "running")
                    await asyncio.to_thread(
                        store.append_event, public_run_id, "run.started", {}
                    )
                if event_type == "approval.requested":
                    request_id = str(
                        data.get("request_id") or data.get("approval_id") or ""
                    )
                    if request_id:
                        data["approval_id"] = await asyncio.to_thread(
                            store.ensure_approval, public_run_id, request_id
                        )
                await asyncio.to_thread(
                    store.append_event, public_run_id, event_type, data
                )
                if (
                    event_type == "tool.completed"
                    and (data.get("tool_name") or data.get("name")) == "cronjob"
                ):
                    await self.reconcile_scheduled_tasks(profile)
                if event_type == "run.started":
                    await asyncio.to_thread(store.update_run, public_run_id, "running")
                elif event_type in {"run.completed", "run.failed", "run.cancelled"}:
                    status = event_type.split(".", 1)[1]
                    await asyncio.to_thread(
                        store.update_run,
                        public_run_id,
                        status,
                        error_code="agent_failed" if status == "failed" else None,
                    )
                    if status != "cancelled":
                        await self._notify(
                            profile,
                            public_run_id,
                            status,
                            response_text=response_text or None,
                        )
                    await self.reconcile_scheduled_tasks(profile)
                elif event_type == "approval.requested":
                    await asyncio.to_thread(
                        store.update_run, public_run_id, "waiting_for_approval"
                    )
                    await self._notify(
                        profile,
                        public_run_id,
                        "approval.requested",
                        data.get("approval_id"),
                    )
                elif event_type == "approval.resolved":
                    await asyncio.to_thread(store.update_run, public_run_id, "running")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Hermes run stream interrupted; REST reconciliation will continue",
                exc_info=False,
            )

    @classmethod
    def _content_text(cls, content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                text
                for item in content
                if (text := cls._content_text(item)).strip()
            )
        if isinstance(content, dict):
            for key in ("text", "content", "value"):
                text = cls._content_text(content.get(key))
                if text.strip():
                    return text
        return ""

    @classmethod
    def _notification_excerpt(cls, content: object) -> str:
        text = " ".join(cls._content_text(content).split())
        if len(text) <= NOTIFICATION_EXCERPT_MAX_CHARS:
            return text
        return text[: NOTIFICATION_EXCERPT_MAX_CHARS - 1].rstrip() + "…"

    async def _completed_notification_copy(
        self,
        profile: str,
        store: ProfileStore,
        run: dict,
        response_text: str | None,
    ) -> tuple[str, str]:
        conversation = await asyncio.to_thread(
            store.conversation, run["conversation_id"]
        )
        title = str((conversation or {}).get("title_override") or "").strip()
        session_id = str((conversation or {}).get("hermes_session_id") or "")

        if not title and session_id:
            try:
                native = await self.facade.get_conversation(profile, session_id)
                session = native.get("session") or native
                title = str(session.get("title") or "").strip()
            except Exception:
                logger.warning(
                    "Could not resolve conversation title for notification",
                    exc_info=False,
                )

        body = self._notification_excerpt(response_text)
        if not body and session_id:
            try:
                native_messages = await self.facade.get_messages(
                    profile, session_id, limit=10, order="latest"
                )
                for message in native_messages.get("data", []):
                    if message.get("role") != "assistant":
                        continue
                    body = self._notification_excerpt(message.get("content"))
                    if body:
                        break
            except Exception:
                logger.warning(
                    "Could not resolve assistant response for notification",
                    exc_info=False,
                )

        return title or "Conversación", body or "La respuesta está lista"

    async def _notify(
        self,
        profile: str,
        run_id: str,
        status: str,
        approval_id: str | None = None,
        *,
        response_text: str | None = None,
    ) -> None:
        store = self.store(profile)
        run = await asyncio.to_thread(store.run, run_id)
        if not run:
            return
        kind = status if status == "approval.requested" else f"run.{status}"
        source_type = "approval" if approval_id else "run"
        source_id = approval_id or run_id
        title = "Aprobación necesaria" if approval_id else "Hermes"
        body = (
            "Se necesita tu aprobación"
            if status == "approval.requested"
            else "La respuesta está lista"
            if status == "completed"
            else "El turno ha fallado"
        )
        if status == "completed" and not approval_id:
            title, body = await self._completed_notification_copy(
                profile, store, run, response_text
            )
        item, _created = await asyncio.to_thread(
            store.create_inbox_item,
            kind=kind,
            severity=(
                "action_required"
                if status == "approval.requested"
                else "error"
                if status == "failed"
                else "info"
            ),
            title=title,
            body=body,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=f"{kind}:{source_id}",
            conversation_id=run["conversation_id"],
            context={
                "run_id": run_id,
                "conversation_id": run["conversation_id"],
                **({"approval_id": approval_id} if approval_id else {}),
            },
        )
        payload = {
            "title": title,
            "body": body,
            "data": {
                "type": kind,
                "profile": profile,
                "conversation_id": run["conversation_id"],
                "run_id": run_id,
                "inbox_item_id": item["public_id"],
                **({"approval_id": approval_id} if approval_id else {}),
            },
        }
        await asyncio.to_thread(
            self.control.enqueue_push, profile, kind, approval_id or run_id, payload
        )

    def extract(self, profile: str, attachment_id: str) -> None:
        self.supervisor.create(
            self._extract(profile, attachment_id), name=f"extract:{attachment_id}"
        )

    async def _extract(self, profile: str, attachment_id: str) -> None:
        store = self.store(profile)
        row = await asyncio.to_thread(store.attachment, attachment_id)
        if not row:
            return
        destination = store.files_root / "extracted" / attachment_id / "content.md"
        try:
            extracted = await asyncio.wait_for(
                asyncio.to_thread(
                    extract_attachment,
                    Path(row["storage_path"]),
                    destination,
                    row["mime_type"],
                ),
                timeout=30,
            )
            await asyncio.to_thread(
                store.update_attachment,
                attachment_id,
                "ready",
                extracted_path=extracted,
            )
        except TimeoutError:
            await asyncio.to_thread(
                store.update_attachment,
                attachment_id,
                "failed",
                error_code="extraction_timeout",
            )
        except ExtractionError as exc:
            code = str(exc)
            status = (
                "needs_ocr"
                if code == "pdf_extractor_unavailable" and self.config.ocr_enabled
                else "failed"
            )
            await asyncio.to_thread(
                store.update_attachment, attachment_id, status, error_code=code
            )
        except Exception:
            await asyncio.to_thread(
                store.update_attachment,
                attachment_id,
                "failed",
                error_code="extraction_failed",
            )

    async def _reconcile_runs(self) -> None:
        while True:
            for profile, store in list(self._stores.items()):
                for run in await asyncio.to_thread(store.nonterminal_runs):
                    try:
                        remote = await self.facade.get_run(
                            profile, run["hermes_run_id"]
                        )
                    except Exception:
                        continue
                    status = str(remote.get("status") or "")
                    if status == "stopping":
                        status = "running"
                    if status in {
                        "queued",
                        "running",
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        previous = run["status"]
                        await asyncio.to_thread(
                            store.update_run,
                            run["public_id"],
                            status,
                            error_code="agent_failed" if status == "failed" else None,
                        )
                        if status in {"completed", "failed"} and previous != status:
                            await self._notify(profile, run["public_id"], status)
                await self.reconcile_scheduled_tasks(profile)
            await asyncio.sleep(5)

    async def reconcile_scheduled_tasks(self, profile: str) -> None:
        """Project native Hermes cron state into the mobile hub."""
        store = self.store(profile)
        try:
            payload = await self.facade.list_scheduled_tasks(
                profile, include_disabled=True
            )
        except Exception:
            return
        for job in payload.get("jobs", []):
            job_id = str(job.get("id") or "")
            if not job_id:
                continue
            origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
            origin_session = (
                str(origin.get("chat_id") or "")
                if origin.get("platform") == "api_server"
                else ""
            )
            conversation = (
                await asyncio.to_thread(store.conversation_by_hermes_id, origin_session)
                if origin_session and origin_session != "api"
                else None
            )
            existing_task = await asyncio.to_thread(
                store.scheduled_task_by_hermes_id, job_id
            )
            task = await asyncio.to_thread(
                store.ensure_scheduled_task,
                job_id,
                conversation["public_id"] if conversation else None,
            )

            # api_server cannot receive Hermes asynchronous deliveries. Keep
            # native execution/output persistence and route the implicit result
            # to the hub instead of inheriting Telegram or a dead API target.
            deliver = str(job.get("deliver") or "")
            if origin.get("platform") == "api_server" and (
                deliver == "origin" or deliver.startswith("api_server")
            ):
                try:
                    updated = await self.facade.update_scheduled_task(
                        profile, job_id, {"deliver": "local"}
                    )
                    job = updated.get("job") or {**job, "deliver": "local"}
                except Exception:
                    logger.warning(
                        "Could not normalize API-origin cron delivery for %s", job_id
                    )

            try:
                executions = await asyncio.to_thread(
                    self.cron_reader.list_executions,
                    self.profile_home(profile),
                    job_id,
                    limit=20,
                )
            except Exception:
                continue
            for execution in executions:
                execution_id = str(execution.get("id") or "")
                if not execution_id:
                    continue
                prior = await asyncio.to_thread(
                    store.scheduled_run_by_hermes_id, execution_id
                )
                run = await asyncio.to_thread(
                    store.ensure_scheduled_run,
                    task["public_id"],
                    execution_id,
                    str(execution.get("claimed_at") or ""),
                )
                if execution.get("status") not in {"completed", "failed", "unknown"}:
                    continue
                status = str(execution.get("status"))
                output = await asyncio.to_thread(
                    self.cron_reader.execution_output,
                    self.profile_home(profile),
                    job_id,
                    execution,
                )
                result_text = output or str(execution.get("error") or "")
                compact_result = " ".join(result_text.split())
                inbox_item, _inbox_created = await asyncio.to_thread(
                    store.create_inbox_item,
                    kind=f"scheduled_run.{status}",
                    severity="info" if status == "completed" else "error",
                    title=str(job.get("name") or "Tarea programada"),
                    body=(
                        compact_result[:240]
                        if compact_result
                        else "El resultado programado está listo."
                        if status == "completed"
                        else "La tarea programada ha fallado."
                    ),
                    source_type="scheduled_run",
                    source_id=run["public_id"],
                    dedupe_key=f"scheduled_run:{execution_id}",
                    occurred_at=str(
                        execution.get("finished_at")
                        or execution.get("claimed_at")
                        or iso()
                    ),
                    conversation_id=task.get("origin_conversation_id"),
                    context={
                        "scheduled_task_id": task["public_id"],
                        "scheduled_run_id": run["public_id"],
                        "task_name": str(job.get("name") or job_id),
                        "status": status,
                        **({"result": result_text[:20_000]} if result_text else {}),
                    },
                )
                if existing_task is None:
                    # Backfill is visible and unread in the hub, but never
                    # replays stale push notifications or chat deliveries.
                    await asyncio.to_thread(
                        store.update_scheduled_run,
                        run["public_id"],
                        {
                            "notification_enqueued_at": iso(),
                            "conversation_delivered_at": iso(),
                        },
                    )
                    continue
                if prior and prior.get("notification_enqueued_at"):
                    continue

                policy = task.get("conversation_policy")
                should_mirror = policy == "origin" or (
                    policy is None and job.get("attach_to_session") is True
                )
                if (
                    should_mirror
                    and conversation
                    and not run.get("conversation_delivered_at")
                ):
                    text = result_text
                    if text.strip():
                        try:
                            await asyncio.to_thread(
                                self.cron_reader.append_conversation_result,
                                self.profile_home(profile),
                                conversation["hermes_session_id"],
                                f"[Cron delivery: {job.get('name') or job_id}]\n{text}",
                            )
                        except Exception:
                            logger.warning(
                                "Could not mirror scheduled result %s", execution_id
                            )
                            continue
                    await asyncio.to_thread(
                        store.update_scheduled_run,
                        run["public_id"],
                        {"conversation_delivered_at": iso()},
                    )

                await asyncio.to_thread(
                    self.control.enqueue_push,
                    profile,
                    f"scheduled_task.{status}",
                    execution_id,
                    {
                        "title": str(job.get("name") or "Tarea programada"),
                        "body": "El resultado programado está listo"
                        if status == "completed"
                        else "La tarea programada ha fallado",
                        "data": {
                            "type": f"scheduled_task.{status}",
                            "profile": profile,
                            "scheduled_task_id": task["public_id"],
                            "scheduled_run_id": run["public_id"],
                            "inbox_item_id": inbox_item["public_id"],
                        },
                    },
                )
                await asyncio.to_thread(
                    store.update_scheduled_run,
                    run["public_id"],
                    {"notification_enqueued_at": iso()},
                )

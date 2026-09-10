from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import MobileConfig
from .files.extraction import ExtractionError, extract_attachment
from .hermes.api_client import HermesAPIClient
from .hermes.cron_reader import NativeCronReader
from .hermes.event_mapper import map_event
from .hermes.profile_preferences import NativeProfilePreferences
from .lifecycle import TaskSupervisor
from .notifications.worker import PushWorker
from .persistence.repositories import ControlStore, ProfileStore, iso
from .security.tokens import SecretBox, TokenManager

logger = logging.getLogger("hermes_mobile")

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
        await self.facade.start()
        self.started = True
        if self.config.push.enabled:
            self.supervisor.create(self.push_worker.run(), name="push-outbox")
        self.supervisor.create(self._reconcile_runs(), name="run-reconciler")

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
        try:
            async for source in self.facade.stream_run_events(profile, hermes_run_id):
                mapped = map_event(source)
                if not mapped:
                    continue
                event_type, data = mapped
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
                        await self._notify(profile, public_run_id, status)
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

    async def _notify(
        self, profile: str, run_id: str, status: str, approval_id: str | None = None
    ) -> None:
        store = self.store(profile)
        run = await asyncio.to_thread(store.run, run_id)
        if not run:
            return
        kind = status if status == "approval.requested" else f"run.{status}"
        payload = {
            "title": "Hermes",
            "body": "Se necesita tu aprobación"
            if status == "approval.requested"
            else "La respuesta está lista"
            if status == "completed"
            else "El turno ha fallado",
            "data": {
                "type": kind,
                "profile": profile,
                "conversation_id": run["conversation_id"],
                "run_id": run_id,
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
            await asyncio.sleep(5)
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
                    output = await asyncio.to_thread(
                        self.cron_reader.execution_output,
                        self.profile_home(profile),
                        job_id,
                        execution,
                    )
                    text = output or str(execution.get("error") or "")
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

                status = str(execution.get("status"))
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
                        },
                    },
                )
                await asyncio.to_thread(
                    store.update_scheduled_run,
                    run["public_id"],
                    {"notification_enqueued_at": iso()},
                )

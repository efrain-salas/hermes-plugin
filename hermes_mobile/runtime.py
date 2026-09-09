from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import MobileConfig
from .files.extraction import ExtractionError, extract_attachment
from .hermes.api_client import HermesAPIClient
from .hermes.event_mapper import map_event
from .lifecycle import TaskSupervisor
from .notifications.worker import PushWorker
from .persistence.repositories import ControlStore, ProfileStore
from .security.tokens import SecretBox, TokenManager

logger = logging.getLogger("hermes_mobile")


class MobileRuntime:
    def __init__(self, config: MobileConfig, facade: HermesAPIClient | None = None):
        self.config = config
        data_root = config.default_home / "plugin-data" / "hermes-mobile"
        self.control = ControlStore(data_root / "control.db")
        self.tokens = TokenManager(data_root / "keys", config.access_token_ttl_seconds)
        self.box = SecretBox(data_root / "keys" / "data-encryption.key")
        self.facade = facade or HermesAPIClient(config.loopback_base_url)
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

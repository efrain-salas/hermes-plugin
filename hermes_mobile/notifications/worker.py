from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import timedelta

from ..config import PushConfig, PushConfigError
from ..persistence.repositories import ControlStore, secret_hash, utcnow
from ..security.tokens import SecretBox
from .apns import (
    APNsPermanentError,
    APNsTemporaryError,
    APNsTokenProvider,
    APNsUnregisteredError,
    build_payload,
    create_apns_client,
    send_apns,
)

logger = logging.getLogger("hermes_mobile")


class PushWorker:
    """Drain the notification outbox through APNs only.

    Expo receipts no longer exist: a ``200`` from APNs marks the row as
    accepted and nothing else is polled afterwards.
    """

    def __init__(self, store: ControlStore, box: SecretBox, config: PushConfig):
        self.store = store
        self.box = box
        self.config = config
        self.client = None
        self.credentials = None
        self.tokens: APNsTokenProvider | None = None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def run(self) -> None:
        if not self.config.enabled:
            return
        try:
            credentials = self.config.credentials()
        except PushConfigError as exc:
            logger.error("APNs push disabled: %s", exc)
            return
        self.credentials = credentials
        self.tokens = APNsTokenProvider(credentials, self.config.jwt_ttl_seconds)
        self.client = create_apns_client(
            self.config.timeout_seconds, http2=self.config.http2
        )
        try:
            while True:
                rows = await asyncio.to_thread(self.store.pending_push, 20)
                if not rows:
                    await asyncio.sleep(1)
                    continue
                for row in rows:
                    await self._send(row)
        finally:
            await self.close()

    async def _send(self, row: dict) -> None:
        attempts = int(row["attempts"])
        environment = row.get("push_environment")
        if row.get("push_provider") != "apns" or not environment:
            await asyncio.to_thread(
                self.store.finish_push, row["id"], error="legacy_provider"
            )
            return
        try:
            token = self.box.decrypt(row["push_token_encrypted"])
        except Exception:  # noqa: BLE001 - corrupted row must not stall the outbox
            await asyncio.to_thread(
                self.store.finish_push, row["id"], error="token_unreadable"
            )
            return
        payload = json.loads(row["payload_json"])
        body = build_payload(payload, self.config.max_payload_bytes)
        try:
            assert self.client is not None and self.credentials and self.tokens
            result = await send_apns(
                self.client,
                self.credentials,
                self.tokens,
                token,
                environment,
                body,
                timeout_seconds=self.config.timeout_seconds,
                endpoint_override=self.config.endpoint_override,
            )
            await asyncio.to_thread(
                self.store.finish_push,
                row["id"],
                accepted=True,
                apns_id=result.apns_id,
            )
        except APNsUnregisteredError as exc:
            # A stale rejection must never revoke a renewed token.
            await asyncio.to_thread(
                self.store.clear_push_token, row["device_id"], secret_hash(token)
            )
            await asyncio.to_thread(
                self.store.finish_push,
                row["id"],
                error=exc.reason,
                apns_id=exc.apns_id,
            )
        except APNsPermanentError as exc:
            await asyncio.to_thread(
                self.store.finish_push,
                row["id"],
                error=exc.reason,
                apns_id=exc.apns_id,
            )
        except APNsTemporaryError as exc:
            await self._retry(row, attempts, exc.reason)
        except Exception as exc:  # noqa: BLE001 - keep the outbox moving
            await self._retry(row, attempts, str(exc)[:80] or "push_failed")

    async def _retry(self, row: dict, attempts: int, code: str) -> None:
        if attempts + 1 >= self.config.max_attempts:
            await asyncio.to_thread(self.store.finish_push, row["id"], error=code)
            return
        delay = min(
            3600, self.config.retry_base_seconds ** min(attempts, 10)
        ) + random.random()
        await asyncio.to_thread(
            self.store.finish_push,
            row["id"],
            error=code,
            retry_at=utcnow() + timedelta(seconds=delay),
        )

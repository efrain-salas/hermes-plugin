from __future__ import annotations

import asyncio
import json
import random
from datetime import timedelta

import aiohttp

from ..config import PushConfig
from ..persistence.repositories import ControlStore, utcnow
from ..security.tokens import SecretBox
from .expo import ExpoPermanentError, check_expo_receipt, send_expo


class PushWorker:
    def __init__(self, store: ControlStore, box: SecretBox, config: PushConfig):
        self.store = store
        self.box = box
        self.config = config
        self.session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def run(self) -> None:
        if not self.config.enabled:
            return
        self.session = aiohttp.ClientSession()
        try:
            while True:
                rows = await asyncio.to_thread(self.store.pending_push, 20)
                receipts = await asyncio.to_thread(self.store.pending_receipts, 20)
                if not rows and not receipts:
                    await asyncio.sleep(1)
                    continue
                for row in rows:
                    await self._send(row)
                for row in receipts:
                    await self._receipt(row)
        finally:
            await self.close()

    async def _send(self, row: dict) -> None:
        attempts = int(row["attempts"])
        try:
            token = self.box.decrypt(row["push_token_encrypted"])
            payload = json.loads(row["payload_json"])
            assert self.session is not None
            ticket = await send_expo(
                self.session,
                self.config.endpoint,
                token,
                payload,
                self.config.timeout_seconds,
            )
            await asyncio.to_thread(self.store.finish_push, row["id"], ticket=ticket)
        except ExpoPermanentError as exc:
            if str(exc) == "DeviceNotRegistered":
                await asyncio.to_thread(
                    self.store.update_device,
                    row["device_id"],
                    {"push_token_encrypted": None},
                )
            await asyncio.to_thread(self.store.finish_push, row["id"], error=str(exc))
        except Exception as exc:
            code = str(exc)[:80] or "push_failed"
            if attempts + 1 >= self.config.max_attempts:
                await asyncio.to_thread(self.store.finish_push, row["id"], error=code)
                return
            delay = min(3600, 2 ** min(attempts, 10)) + random.random()
            await asyncio.to_thread(
                self.store.finish_push,
                row["id"],
                error=code,
                retry_at=utcnow() + timedelta(seconds=delay),
            )

    async def _receipt(self, row: dict) -> None:
        try:
            assert self.session is not None
            await check_expo_receipt(
                self.session,
                self.config.endpoint,
                row["provider_ticket_id"],
                self.config.timeout_seconds,
            )
            await asyncio.to_thread(self.store.finish_receipt, row["id"])
        except ExpoPermanentError as exc:
            if str(exc) == "DeviceNotRegistered":
                await asyncio.to_thread(
                    self.store.update_device,
                    row["device_id"],
                    {"push_token_encrypted": None},
                )
            await asyncio.to_thread(
                self.store.finish_receipt, row["id"], error=str(exc)
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.store.finish_receipt,
                row["id"],
                error=str(exc)[:80] or "receipt_failed",
                retry_at=utcnow() + timedelta(seconds=30),
            )

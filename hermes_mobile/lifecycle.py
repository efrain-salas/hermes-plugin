from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("hermes_mobile")


class TaskSupervisor:
    def __init__(self):
        self.tasks: set[asyncio.Task] = set()
        self.closing = False

    def create(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        if self.closing:
            coroutine.close()
            raise RuntimeError("supervisor is closing")
        task = asyncio.create_task(coroutine, name=f"hermes-mobile:{name}")
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            logger.error("supervised task failed: %s", type(error).__name__)

    async def close(self) -> None:
        self.closing = True
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

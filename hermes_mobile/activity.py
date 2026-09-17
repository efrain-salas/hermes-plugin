"""Durable activity snapshot for host-side observers.

The gateway only rewrites ``gateway_state.json`` at lifecycle transitions and
messaging-channel turn boundaries, so agent work owned by the API server (native
``/v1/runs`` and session chats) or the cron scheduler never moves that file. This
module publishes a small, self-describing JSON document into the host-mounted
``plugin-data`` directory on a fixed heartbeat so an out-of-container monitor (the
UGREEN LED daemon) can read live agent activity without container access or an
authenticated endpoint.

Two signals are merged:

* The plugin's own in-flight runs (native mirrored runs and in-process quick
  runs), tracked by explicit ``begin``/``end`` calls.
* The gateway process's live work count when the runner is reachable, which also
  covers Telegram/all messaging channels, API session chats and cron jobs.

The file is written atomically and refreshed on every change plus a heartbeat, so
a stale ``updated_at`` reliably means the writer died mid-run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes_mobile")

ACTIVITY_FILE_VERSION = 1
ACTIVITY_KIND = "hermes-mobile-activity"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def gateway_active_count(runner: object | None) -> int | None:
    """Return the gateway's live agent count, or ``None`` when unreadable.

    Prefers the aggregate ``_active_work_count`` used by the shutdown drain.
    Falls back to summing the individual private counters so a future rename of
    a single accessor does not blank the signal. Every accessor is duck-typed
    and best-effort: an incompatible Hermes release degrades to ``None`` rather
    than breaking agent turns.
    """
    if runner is None:
        return None
    counter = getattr(runner, "_active_work_count", None)
    if callable(counter):
        try:
            return max(0, int(counter()))
        except Exception:
            logger.debug("gateway active-work probe failed", exc_info=False)
    partials = (
        "_running_agent_count",
        "_active_cron_job_count",
        "_active_api_run_count",
        "_active_deferred_agent_worker_count",
    )
    total = 0
    seen = False
    for name in partials:
        method = getattr(runner, name, None)
        if not callable(method):
            continue
        try:
            total += max(0, int(method()))
            seen = True
        except Exception:
            continue
    return total if seen else None


class ActivityPublisher:
    """Track in-flight runs and mirror the live count to a JSON file."""

    def __init__(self, path: Path, *, boot_id: str) -> None:
        self.path = path
        self.boot_id = boot_id
        self._runs: dict[str, dict[str, Any]] = {}
        self._runner: object | None = None

    def bind_gateway(self, runner: object | None) -> None:
        """Attach the gateway runner whose live work count we surface."""
        self._runner = runner

    @property
    def active_runs(self) -> int:
        return len(self._runs)

    def begin(self, run_id: str, profile: str) -> None:
        if not run_id:
            return
        self._runs[run_id] = {
            "profile": str(profile or ""),
            "started_at": _utc_now_iso(),
        }
        self.write()

    def end(self, run_id: str) -> None:
        if self._runs.pop(run_id, None) is not None:
            self.write()

    def snapshot(self) -> dict[str, Any]:
        gateway_count = gateway_active_count(self._runner)
        profiles: dict[str, int] = {}
        for run in self._runs.values():
            profile = str(run.get("profile") or "")
            profiles[profile] = profiles.get(profile, 0) + 1
        plugin_runs = len(self._runs)
        active = max(gateway_count or 0, plugin_runs)
        return {
            "version": ACTIVITY_FILE_VERSION,
            "kind": ACTIVITY_KIND,
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "updated_at": _utc_now_iso(),
            "active_agents": active,
            "source": "gateway" if gateway_count is not None else "plugin",
            "gateway_active_agents": gateway_count,
            "plugin_active_runs": plugin_runs,
            "profiles": profiles,
        }

    def write(self) -> bool:
        """Atomically publish the snapshot; never raise into a turn."""
        payload = self.snapshot()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".activity.",
                suffix=".json",
                delete=False,
            )
            temporary = Path(handle.name)
            try:
                with handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                os.chmod(temporary, 0o644)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return True
        except OSError:
            logger.warning("Could not publish Hermes Mobile activity file", exc_info=False)
            return False

    def reset(self) -> None:
        """Forget in-flight runs and publish an idle snapshot on shutdown."""
        self._runs.clear()
        self.write()

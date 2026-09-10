from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any


class NativeCronReader:
    """Thin in-process adapter over Hermes' native cron stores.

    Scheduling, validation, claiming, execution state and output persistence stay
    owned by Hermes.  This adapter only reads those stores and writes the two
    metadata fields that the older loopback jobs API does not expose.
    """

    @staticmethod
    @contextlib.contextmanager
    def _scope(profile_home: Path) -> Iterator[Any]:
        from cron import jobs
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(str(profile_home))
        try:
            with jobs.use_cron_store(profile_home):
                yield jobs
        finally:
            reset_hermes_home_override(token)

    def list_executions(
        self,
        profile_home: Path,
        job_id: str,
        *,
        limit: int = 50,
        before_claimed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._scope(profile_home):
            from cron.executions import list_executions

            return list_executions(
                job_id=job_id,
                limit=limit,
                before_claimed_at=before_claimed_at,
            )

    def get_execution(
        self, profile_home: Path, execution_id: str
    ) -> dict[str, Any] | None:
        with self._scope(profile_home):
            from cron.executions import get_execution

            return get_execution(execution_id)

    def update_job_metadata(
        self, profile_home: Path, job_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {"attach_to_session", "origin"}
        clean = {key: value for key, value in updates.items() if key in allowed}
        if not clean:
            return None
        with self._scope(profile_home) as jobs:
            return jobs.update_job(job_id, clean)

    def execution_output(
        self,
        profile_home: Path,
        job_id: str,
        execution: dict[str, Any],
    ) -> str | None:
        """Return the native output file closest to this execution's finish.

        Hermes writes the output immediately before making the execution ledger
        terminal.  Matching by mtime avoids duplicating Hermes' output index or
        relying on the locale-dependent filename timestamp.
        """
        finished = execution.get("finished_at")
        if not isinstance(finished, str):
            return None
        try:
            target = datetime.fromisoformat(finished).timestamp()
        except ValueError:
            return None
        directory = profile_home / "cron" / "output" / job_id
        try:
            candidates = [path for path in directory.glob("*.md") if path.is_file()]
            path = min(candidates, key=lambda item: abs(item.stat().st_mtime - target))
            # Output is saved before the terminal ledger update.  A five-minute
            # tolerance accommodates slow delivery without pairing unrelated runs.
            if abs(path.stat().st_mtime - target) > 300:
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None

    def append_conversation_result(
        self, profile_home: Path, session_id: str, text: str
    ) -> None:
        """Mirror a result using Hermes' native cron transcript convention."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=profile_home / "state.db")
        try:
            # Native cron mirrors are labelled user rows: this preserves strict
            # role alternation and makes the result context for the next turn.
            db.append_message(session_id, "user", content=text)
        finally:
            db.close()

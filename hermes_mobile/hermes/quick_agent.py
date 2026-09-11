"""Lightweight in-process agent for quick turns.

Hermes' HTTP API always builds the full harness (memory, context files, skills,
MCP, end-of-turn review). This module builds an :class:`AIAgent` directly with
those pieces disabled and only the configured web-search toolset enabled, while
still persisting the turn to the profile SessionDB so the conversation keeps its
history. All Hermes imports stay lazy so the plugin can load in any CLI context.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("hermes_mobile")


class QuickAgentUnavailable(RuntimeError):
    """The in-process lightweight agent is unavailable in this Hermes build."""


def build_quick_system_prompt(
    *, timezone: str | None = None, locale: str | None = None, now: datetime | None = None
) -> str:
    """Minimal system prompt: date/time, optional location, concise-answer guidance."""
    moment = now or datetime.now(UTC)
    lines = [
        "Eres un asistente rápido para consultas simples.",
        (
            "Responde de forma directa y concisa. Usa la búsqueda web cuando la "
            "respuesta dependa de información actual."
        ),
        f"Fecha y hora actuales: {moment.isoformat(timespec='minutes')}.",
    ]
    if timezone:
        lines.append(f"Zona horaria del usuario: {timezone}.")
    if locale:
        lines.append(f"Idioma preferido del usuario: {locale}.")
    return "\n".join(lines)


def _translate_tool_event(
    event_type: str, tool_name: Any, preview: Any, kwargs: dict[str, Any]
) -> dict[str, Any] | None:
    """Map AIAgent tool-progress callbacks to Hermes wire events (see event_mapper)."""
    if event_type == "tool.started":
        return {"event": "tool.started", "tool_name": tool_name, "preview": preview}
    if event_type == "tool.completed":
        try:
            duration = round(float(kwargs.get("duration") or 0), 3)
        except (TypeError, ValueError):
            duration = 0
        return {
            "event": "tool.completed",
            "tool_name": tool_name,
            "duration": duration,
            "is_error": bool(kwargs.get("is_error", False)),
        }
    if event_type == "reasoning.available":
        return {"event": "reasoning.available", "text": preview or ""}
    return None


class QuickRunControl:
    """Thread-safe cancel handle for one in-process quick run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent: Any = None
        self._cancelled = False

    def attach(self, agent: Any) -> None:
        with self._lock:
            self._agent = agent
            cancelled = self._cancelled
        if cancelled:
            self._interrupt(agent)

    def detach(self) -> None:
        with self._lock:
            self._agent = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            agent = self._agent
        if agent is not None:
            self._interrupt(agent)

    @staticmethod
    def _interrupt(agent: Any) -> None:
        try:
            from agent.interrupt_compat import request_hard_interrupt

            request_hard_interrupt(agent, "Quick run cancelled")
        except Exception:
            logger.debug("Quick run cancel failed", exc_info=True)


class NativeQuickAgent:
    """Run one turn with memory, context files, review and MCP disabled."""

    def __init__(
        self,
        *,
        toolsets: tuple[str, ...] | list[str],
        max_iterations: int,
        timeout_seconds: int,
        agent_factory: Any = None,
        session_loader: Any = None,
        profile_scope: Any = None,
        runtime_resolver: Any = None,
    ):
        self.toolsets = tuple(toolsets)
        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds
        self._agent_factory = agent_factory
        self._session_loader = session_loader
        self._profile_scope = profile_scope
        self._runtime_resolver = runtime_resolver

    @staticmethod
    def _default_profile_scope(profile: str) -> Any:
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_profile_dir

        return _profile_runtime_scope(get_profile_dir(profile))

    @staticmethod
    def _default_runtime_resolver(session_model: str) -> tuple[dict[str, Any], str]:
        """Resolve provider credentials and the effective model for this profile.

        Mirrors the API server's ``_create_agent``: without this the in-process
        agent would build a client with no provider/base_url/api_key and the
        upstream call fails. Must run inside the profile scope.
        """
        from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs

        runtime_kwargs = dict(_resolve_runtime_agent_kwargs() or {})
        configured = runtime_kwargs.pop("model", None) or _resolve_gateway_model()
        return runtime_kwargs, (session_model or configured)

    async def stream_events(
        self,
        *,
        profile: str,
        session_id: str,
        user_message: str,
        model: str = "",
        reasoning_effort: str | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        control: QuickRunControl | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield Hermes-shaped events for one quick turn, then a terminal event."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()

        def emit(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def _worker() -> None:
            try:
                self._run(
                    profile=profile,
                    session_id=session_id,
                    user_message=user_message,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    timezone=timezone,
                    locale=locale,
                    control=control,
                    emit=emit,
                )
            except BaseException as exc:  # noqa: BLE001 - surface as a wire event
                logger.warning("Quick run %s crashed: %s", session_id, exc)
                emit({"event": "run.failed", "error": str(exc)[:500]})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        task = asyncio.ensure_future(asyncio.to_thread(_worker))
        deadline = loop.time() + self.timeout_seconds
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if control is not None:
                        control.cancel()
                    yield {"event": "run.failed", "error": "quick run timed out"}
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    if control is not None:
                        control.cancel()
                    yield {"event": "run.failed", "error": "quick run timed out"}
                    break
                if item is sentinel:
                    break
                yield item
        finally:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except TimeoutError:
                logger.warning("Quick run worker did not stop after cancel")

    def _run(
        self,
        *,
        profile: str,
        session_id: str,
        user_message: str,
        model: str,
        reasoning_effort: str | None,
        timezone: str | None,
        locale: str | None,
        control: QuickRunControl | None,
        emit: Any,
    ) -> None:
        scope_factory = self._profile_scope or self._default_profile_scope
        loader = self._session_loader or self._load_session
        resolver = self._runtime_resolver or self._default_runtime_resolver
        with scope_factory(profile):
            session_db, history, session_model = loader(session_id, model)
            runtime_kwargs, resolved_model = resolver(session_model)
            agent = self._build_agent(
                model=resolved_model,
                runtime_kwargs=runtime_kwargs,
                session_id=session_id,
                session_db=session_db,
                system_prompt=build_quick_system_prompt(timezone=timezone, locale=locale),
                reasoning_effort=reasoning_effort,
                emit=emit,
            )
            if control is not None:
                control.attach(agent)
            try:
                result = agent.run_conversation(
                    user_message=user_message,
                    conversation_history=history,
                    task_id=session_id,
                )
            finally:
                if control is not None:
                    control.detach()

        usage = {
            "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
            "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
            "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
        }
        if not isinstance(result, dict):
            result = {}
        if control is not None and control.cancelled:
            return
        if result.get("failed"):
            emit(
                {
                    "event": "run.failed",
                    "error": str(result.get("error") or "quick run failed")[:500],
                }
            )
            return
        final = result.get("final_response")
        if isinstance(final, str) and final:
            emit({"event": "message.completed", "content": final})
        emit({"event": "run.completed", "usage": usage})

    @staticmethod
    def _load_session(session_id: str, model: str) -> tuple[Any, list[dict[str, Any]], str]:
        session_db = None
        try:
            from hermes_state_registry import acquire

            session_db = acquire()
        except Exception:
            logger.debug("Quick run: SessionDB unavailable", exc_info=True)
            return None, [], model
        history: list[dict[str, Any]] = []
        resolved = model
        try:
            history = session_db.get_messages_as_conversation(session_id) or []
        except Exception:
            logger.debug("Quick run: history load failed", exc_info=True)
        try:
            row = session_db.get_session(session_id) or {}
            resolved = resolved or str(row.get("model") or "")
        except Exception:
            logger.debug("Quick run: session model lookup failed", exc_info=True)
        return session_db, history, resolved

    def _build_agent(
        self,
        *,
        model: str,
        runtime_kwargs: dict[str, Any],
        session_id: str,
        session_db: Any,
        system_prompt: str,
        reasoning_effort: str | None,
        emit: Any,
    ) -> Any:
        def _delta(delta: Any) -> None:
            if isinstance(delta, str) and delta:
                emit({"event": "message.delta", "delta": delta})

        def _tool(event_type: str, tool_name: Any = None, preview: Any = None, args: Any = None, **kwargs: Any) -> None:
            event = _translate_tool_event(event_type, tool_name, preview, kwargs)
            if event:
                emit(event)

        # Quick turns never reason: the lightweight agent deliberately ignores
        # the conversation's reasoning preference and always disables it.
        reasoning_config = {"enabled": False}

        kwargs: dict[str, Any] = {
            "model": model,
            **runtime_kwargs,
            "max_iterations": self.max_iterations,
            "quiet_mode": True,
            "verbose_logging": False,
            "platform": "api_server",
            "session_id": session_id,
            "session_db": session_db,
            "enabled_toolsets": list(self.toolsets),
            "skip_memory": True,
            "skip_context_files": True,
            "skip_background_review": True,
            "ephemeral_system_prompt": system_prompt,
            "stream_delta_callback": _delta,
            "tool_progress_callback": _tool,
        }
        kwargs["reasoning_config"] = reasoning_config
        factory = self._agent_factory or self._default_agent_factory
        return factory(**kwargs)

    @staticmethod
    def _default_agent_factory(**kwargs: Any) -> Any:
        from run_agent import AIAgent

        return AIAgent(**kwargs)

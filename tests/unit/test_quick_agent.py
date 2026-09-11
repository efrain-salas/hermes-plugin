from __future__ import annotations

import contextlib
import sys
import types
from datetime import UTC, datetime

from hermes_mobile.hermes.quick_agent import (
    NativeQuickAgent,
    QuickRunControl,
    _translate_tool_event,
    build_quick_system_prompt,
)


def test_build_quick_system_prompt_includes_context() -> None:
    prompt = build_quick_system_prompt(
        timezone="Europe/Madrid",
        locale="es-ES",
        now=datetime(2026, 9, 11, 10, 30, tzinfo=UTC),
    )
    assert "2026-09-11T10:30" in prompt
    assert "Europe/Madrid" in prompt
    assert "es-ES" in prompt


def test_build_quick_system_prompt_without_optional_context() -> None:
    prompt = build_quick_system_prompt(now=datetime(2026, 1, 1, tzinfo=UTC))
    assert "Zona horaria" not in prompt
    assert "Idioma preferido" not in prompt


def test_translate_tool_event_variants() -> None:
    assert _translate_tool_event("tool.started", "web_search", "buscando", {}) == {
        "event": "tool.started",
        "tool_name": "web_search",
        "preview": "buscando",
    }
    assert _translate_tool_event(
        "tool.completed", "web_search", None, {"duration": "0.25", "is_error": True}
    ) == {
        "event": "tool.completed",
        "tool_name": "web_search",
        "duration": 0.25,
        "is_error": True,
    }
    assert _translate_tool_event(
        "tool.completed", "web_search", None, {"duration": None}
    )["duration"] == 0
    assert _translate_tool_event(
        "tool.completed", "web_search", None, {"duration": "n/a"}
    )["duration"] == 0
    assert _translate_tool_event("reasoning.available", None, "resumen", {}) == {
        "event": "reasoning.available",
        "text": "resumen",
    }
    assert _translate_tool_event("unknown.event", "x", None, {}) is None


def test_quick_run_control_cancel_interrupts_attached_agent(monkeypatch) -> None:
    interrupted: list[object] = []
    monkeypatch.setattr(
        QuickRunControl, "_interrupt", staticmethod(lambda agent: interrupted.append(agent))
    )
    control = QuickRunControl()
    control.cancel()  # cancelled before the agent exists
    agent = object()
    control.attach(agent)  # late attach still interrupts
    assert control.cancelled is True
    assert interrupted == [agent]
    control.detach()
    control.cancel()
    assert interrupted == [agent]  # no agent attached -> no second interrupt


class FakeAgent:
    session_prompt_tokens = 123
    session_completion_tokens = 45
    session_total_tokens = 168

    def __init__(self, *, result=None, raise_exc=None, on_run=None, **kwargs):
        self.kwargs = kwargs
        self.result = result if result is not None else {
            "final_response": "respuesta final",
            "completed": True,
        }
        self.raise_exc = raise_exc
        self.on_run = on_run
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(self, user_message, conversation_history=None, task_id=None):
        if self.on_run is not None:
            self.on_run(self)
        if self.raise_exc is not None:
            raise self.raise_exc
        self.stream_delta_callback("hola ")
        self.stream_delta_callback("mundo")
        self.tool_progress_callback("tool.started", "web_search", "buscando")
        self.tool_progress_callback(
            "tool.completed", "web_search", None, None, duration=0.2, is_error=False
        )
        self.tool_progress_callback("reasoning.available", None, "resumen")
        self.tool_progress_callback("noise.event", "x")
        return self.result


_USAGE = {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168}


class FakeFactory:
    def __init__(self, **overrides):
        self.overrides = overrides
        self.instances: list[FakeAgent] = []

    def __call__(self, **kwargs):
        instance = FakeAgent(**{**kwargs, **self.overrides})
        self.instances.append(instance)
        return instance


def _loader(session_id, model):
    return (None, [{"role": "user", "content": "anterior"}], model or "resolved-model")


def _runtime_resolver(session_model):
    return {"provider": "mock-provider", "base_url": "http://mock", "api_key": "k"}, (
        session_model or "configured-model"
    )


def _scope(_profile):
    return contextlib.nullcontext()


def _agent(factory, *, timeout_seconds=30, loader=_loader):
    return NativeQuickAgent(
        toolsets=["search"],
        max_iterations=4,
        timeout_seconds=timeout_seconds,
        agent_factory=factory,
        session_loader=loader,
        profile_scope=_scope,
        runtime_resolver=_runtime_resolver,
    )


async def _collect(agent, control=None, reasoning_effort="low"):
    return [
        event
        async for event in agent.stream_events(
            profile="default",
            session_id="s1",
            user_message="¿qué pasa?",
            reasoning_effort=reasoning_effort,
            timezone="Europe/Madrid",
            locale="es-ES",
            control=control,
        )
    ]


async def test_stream_events_emits_light_agent_events() -> None:
    factory = FakeFactory()
    events = await _collect(_agent(factory))
    types = [event["event"] for event in events]
    assert types == [
        "message.delta",
        "message.delta",
        "tool.started",
        "tool.completed",
        "reasoning.available",
        "message.completed",
        "run.completed",
    ]
    assert events[-1] == {"event": "run.completed", "usage": _USAGE}
    kwargs = factory.instances[0].kwargs
    assert kwargs["enabled_toolsets"] == ["search"]
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_context_files"] is True
    assert kwargs["skip_background_review"] is True
    assert kwargs["platform"] == "api_server"
    assert kwargs["session_id"] == "s1"
    assert kwargs["provider"] == "mock-provider"
    assert kwargs["base_url"] == "http://mock"
    assert kwargs["reasoning_config"] == {"enabled": False}
    assert kwargs["ephemeral_system_prompt"].startswith("Eres un asistente rápido")


async def test_stream_events_reports_failed_result() -> None:
    factory = FakeFactory(result={"failed": True, "error": "provider down"})
    events = await _collect(_agent(factory))
    assert events[-1] == {"event": "run.failed", "error": "provider down"}


async def test_stream_events_reports_worker_crash() -> None:
    factory = FakeFactory(raise_exc=RuntimeError("kaboom"))
    events = await _collect(_agent(factory))
    assert events[-1]["event"] == "run.failed"
    assert "kaboom" in events[-1]["error"]


async def test_stream_events_cancel_suppresses_terminal_event() -> None:
    control = QuickRunControl()
    factory = FakeFactory(on_run=lambda _agent: control.cancel())
    events = await _collect(_agent(factory), control=control)
    assert "run.completed" not in [event["event"] for event in events]
    assert control.cancelled is True


async def test_stream_events_times_out() -> None:
    factory = FakeFactory()
    events = await _collect(_agent(factory, timeout_seconds=0))
    assert events == [{"event": "run.failed", "error": "quick run timed out"}]


async def test_stream_events_reasoning_disabled() -> None:
    factory = FakeFactory(result={"final_response": None, "completed": True})
    events = await _collect(_agent(factory), reasoning_effort="none")
    assert factory.instances[0].kwargs["reasoning_config"] == {"enabled": False}
    assert "message.completed" not in [event["event"] for event in events]
    assert events[-1] == {"event": "run.completed", "usage": _USAGE}


async def test_stream_events_without_reasoning_effort() -> None:
    factory = FakeFactory()
    events = await _collect(_agent(factory), reasoning_effort=None)
    assert factory.instances[0].kwargs["reasoning_config"] == {"enabled": False}
    assert events[-1] == {"event": "run.completed", "usage": _USAGE}


def test_load_session_reads_history_and_model(monkeypatch) -> None:
    class DB:
        def get_messages_as_conversation(self, _sid):
            return [{"role": "user", "content": "x"}]

        def get_session(self, _sid):
            return {"model": "deepseek-v4"}

    module = types.ModuleType("hermes_state_registry")
    module.acquire = lambda: DB()
    monkeypatch.setitem(sys.modules, "hermes_state_registry", module)
    session_db, history, model = NativeQuickAgent._load_session("s1", "")
    assert session_db is not None
    assert history == [{"role": "user", "content": "x"}]
    assert model == "deepseek-v4"


def test_load_session_survives_db_failures(monkeypatch) -> None:
    class DB:
        def get_messages_as_conversation(self, _sid):
            raise RuntimeError("no history")

        def get_session(self, _sid):
            raise RuntimeError("no session")

    module = types.ModuleType("hermes_state_registry")
    module.acquire = lambda: DB()
    monkeypatch.setitem(sys.modules, "hermes_state_registry", module)
    session_db, history, model = NativeQuickAgent._load_session("s1", "given")
    assert session_db is not None
    assert history == []
    assert model == "given"


def test_default_profile_scope_uses_hermes_modules(monkeypatch) -> None:
    calls: list[str] = []

    def fake_scope(home):
        calls.append(home)
        return contextlib.nullcontext()

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    gateway_run = types.ModuleType("gateway.run")
    gateway_run._profile_runtime_scope = fake_scope
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.get_profile_dir = lambda profile: f"/home/{profile}"
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    with NativeQuickAgent._default_profile_scope("default"):
        pass
    assert calls == ["/home/default"]


def test_default_runtime_resolver_prefers_session_model(monkeypatch) -> None:
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    gateway_run = types.ModuleType("gateway.run")
    gateway_run._resolve_runtime_agent_kwargs = lambda: {
        "provider": "p",
        "model": "cfg-model",
    }
    gateway_run._resolve_gateway_model = lambda: "fallback"
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    kwargs, model = NativeQuickAgent._default_runtime_resolver("session-model")
    assert kwargs == {"provider": "p"}
    assert model == "session-model"
    _kwargs, configured = NativeQuickAgent._default_runtime_resolver("")
    assert configured == "cfg-model"


def test_load_session_without_hermes_runtime() -> None:
    # hermes_state_registry is only importable inside Hermes; the loader must degrade.
    assert NativeQuickAgent._load_session("s1", "fallback-model") == (
        None,
        [],
        "fallback-model",
    )

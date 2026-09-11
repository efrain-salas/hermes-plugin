from __future__ import annotations

import copy
import sys
import types

from hermes_mobile.hermes.profile_preferences import NativeProfilePreferences


def test_native_profile_preferences_preserve_provider_and_update_defaults(
    tmp_path, monkeypatch
):
    state = {
        "model": {
            "default": "old-model",
            "provider": "openai-codex",
            "base_url": "http://provider.test",
        },
        "agent": {"reasoning_effort": "medium", "max_turns": 42},
        "unrelated": {"keep": True},
    }
    writes: list[set[tuple[str, ...]]] = []
    scopes: list[tuple[str, object]] = []

    config_module = types.ModuleType("hermes_cli.config")

    def load_config():
        return copy.deepcopy(state)

    def save_config(config, *, preserve_keys):
        state.clear()
        state.update(copy.deepcopy(config))
        writes.append(set(preserve_keys))

    config_module.load_config = load_config
    config_module.save_config = save_config
    cli_module = types.ModuleType("hermes_cli")
    cli_module.__path__ = []
    constants_module = types.ModuleType("hermes_constants")

    def set_home(path):
        token = object()
        scopes.append((path, token))
        return token

    def reset_home(token):
        scopes.append(("reset", token))

    def resolve_reasoning(config, _model):
        effort = (config.get("agent") or {}).get("reasoning_effort")
        if not effort:
            return None
        if effort == "none":
            return {"enabled": False}
        return {"enabled": True, "effort": effort}

    constants_module.set_hermes_home_override = set_home
    constants_module.reset_hermes_home_override = reset_home
    constants_module.resolve_reasoning_config = resolve_reasoning
    monkeypatch.setitem(sys.modules, "hermes_cli", cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants_module)

    manager = NativeProfilePreferences()
    updated = manager.update(
        tmp_path,
        model="new-model",
        update_model=True,
        reasoning_effort="high",
        update_reasoning=True,
        quick_model="quick-model",
        update_quick_model=True,
    )
    assert updated == {
        "model": "new-model",
        "provider": "openai-codex",
        "quick_model": "quick-model",
        "reasoning_effort": "high",
    }
    assert state["model"] == {
        "default": "new-model",
        "provider": "openai-codex",
        "base_url": "http://provider.test",
        "quick": "quick-model",
    }
    assert state["agent"] == {"reasoning_effort": "high", "max_turns": 42}
    assert state["unrelated"] == {"keep": True}
    assert writes[-1] == {
        ("model", "default"),
        ("model", "quick"),
        ("agent", "reasoning_effort"),
    }

    cleared = manager.update(
        tmp_path,
        model=None,
        update_model=False,
        reasoning_effort=None,
        update_reasoning=True,
        quick_model=None,
        update_quick_model=True,
    )
    assert cleared["reasoning_effort"] is None
    assert cleared["quick_model"] is None
    assert "reasoning_effort" not in state["agent"]
    assert "quick" not in state["model"]
    assert scopes[0][0] == str(tmp_path)
    assert any(scope == "reset" for scope, _token in scopes)

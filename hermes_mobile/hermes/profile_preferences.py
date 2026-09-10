from __future__ import annotations

import contextlib
import copy
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes_mobile.profile_preferences")


class NativeProfilePreferences:
    """Read and update Hermes' profile-scoped model preferences in-process."""

    _lock = threading.Lock()

    @staticmethod
    @contextlib.contextmanager
    def _scope(profile_home: Path) -> Iterator[tuple[Any, Any]]:
        from hermes_cli.config import load_config, save_config
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(str(profile_home))
        try:
            yield load_config, save_config
        finally:
            reset_hermes_home_override(token)

    def read(self, profile_home: Path, *, model: str = "") -> dict[str, Any]:
        with self._scope(profile_home) as (load_config, _save_config):
            config = load_config()
            model_config = config.get("model")
            if isinstance(model_config, dict):
                default_model = str(
                    model_config.get("default") or model_config.get("model") or ""
                ).strip()
                provider = str(model_config.get("provider") or "").strip()
            else:
                default_model = str(model_config or "").strip()
                provider = ""

            from hermes_constants import resolve_reasoning_config

            reasoning = resolve_reasoning_config(config, model or default_model)
            if reasoning and reasoning.get("enabled") is False:
                effort = "none"
            elif reasoning and reasoning.get("enabled") is True:
                effort = reasoning.get("effort")
            else:
                effort = None
            return {
                "model": default_model or None,
                "provider": provider or None,
                "reasoning_effort": effort,
            }

    def update(
        self,
        profile_home: Path,
        *,
        model: str | None,
        update_model: bool,
        reasoning_effort: str | None,
        update_reasoning: bool,
    ) -> dict[str, Any]:
        with self._lock, self._scope(profile_home) as (load_config, save_config):
            config = load_config()
            previous = copy.deepcopy(config)
            preserve_keys: set[tuple[str, ...]] = set()
            if update_model:
                model_config = config.get("model")
                if not isinstance(model_config, dict):
                    model_config = {"default": str(model_config or "").strip()}
                model_config["default"] = str(model or "").strip()
                config["model"] = model_config
                preserve_keys.add(("model", "default"))

            if update_reasoning:
                agent_config = config.get("agent")
                if not isinstance(agent_config, dict):
                    agent_config = {}
                if reasoning_effort is None:
                    agent_config.pop("reasoning_effort", None)
                else:
                    agent_config["reasoning_effort"] = reasoning_effort
                config["agent"] = agent_config
                preserve_keys.add(("agent", "reasoning_effort"))

            try:
                save_config(config, preserve_keys=preserve_keys)
                persisted = load_config()
                persisted_model = persisted.get("model")
                if isinstance(persisted_model, dict):
                    stored_model = str(
                        persisted_model.get("default")
                        or persisted_model.get("model")
                        or ""
                    ).strip()
                else:
                    stored_model = str(persisted_model or "").strip()
                persisted_agent = persisted.get("agent")
                stored_reasoning = (
                    persisted_agent.get("reasoning_effort")
                    if isinstance(persisted_agent, dict)
                    else None
                )
                stored_reasoning = stored_reasoning or None
                if update_model and stored_model != model:
                    raise RuntimeError(
                        "Hermes did not persist the profile model preference"
                    )
                if update_reasoning and stored_reasoning != reasoning_effort:
                    raise RuntimeError(
                        "Hermes did not persist the profile reasoning preference"
                    )
            except Exception:
                try:
                    save_config(previous, preserve_keys=preserve_keys)
                except Exception:
                    logger.exception(
                        "Could not restore Hermes profile preferences after write failure"
                    )
                raise

        return self.read(profile_home, model=model or "")

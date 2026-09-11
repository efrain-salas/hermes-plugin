from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, parsed))


@dataclass(frozen=True)
class PushConfig:
    enabled: bool = True
    endpoint: str = "https://exp.host/--/api/v2/push/send"
    timeout_seconds: int = 10
    max_attempts: int = 8


@dataclass(frozen=True)
class MobileConfig:
    default_home: Path
    public_base_url: str = ""
    loopback_base_url: str = "http://127.0.0.1:8642"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 90
    pairing_ttl_seconds: int = 600
    max_file_bytes: int = 50 * 1024 * 1024
    max_attachments_per_turn: int = 10
    max_text_chars: int = 100_000
    event_retention_hours: int = 24
    terminal_event_retention_days: int = 7
    sse_per_device: int = 3
    push: PushConfig = field(default_factory=PushConfig)
    files_enabled: bool = True
    ocr_enabled: bool = False
    quick_enabled: bool = True
    quick_toolsets: tuple[str, ...] = ("search",)
    quick_max_iterations: int = 8
    quick_timeout_seconds: int = 180

    @classmethod
    def from_context(cls, ctx: Any) -> MobileConfig:
        try:
            from hermes_constants import get_default_hermes_root

            default_home = Path(get_default_hermes_root())
        except Exception:  # noqa: BLE001 - compatibility across Hermes releases
            import os

            default_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        if default_home.parent.name == "profiles":
            default_home = default_home.parent.parent
        push = ctx.get_config("push", {}) or {}
        files = ctx.get_config("files", {}) or {}
        quick = ctx.get_config("quick", {}) or {}
        if not isinstance(push, Mapping):
            push = {}
        if not isinstance(files, Mapping):
            files = {}
        if not isinstance(quick, Mapping):
            quick = {}
        quick_toolsets = quick.get("toolsets", ("search",))
        if isinstance(quick_toolsets, str):
            quick_toolsets = (quick_toolsets,)
        if not isinstance(quick_toolsets, (list, tuple)):
            quick_toolsets = ("search",)
        quick_toolsets = tuple(
            str(name).strip() for name in quick_toolsets if str(name).strip()
        ) or ("search",)
        return cls(
            default_home=default_home,
            public_base_url=str(ctx.get_config("public_base_url", "") or "").rstrip(
                "/"
            ),
            loopback_base_url=str(
                ctx.get_config("loopback_base_url", "http://127.0.0.1:8642")
            ).rstrip("/"),
            access_token_ttl_seconds=_bounded_int(
                ctx.get_config("access_token_ttl_seconds", 900), 900, 60, 3600
            ),
            refresh_token_ttl_days=_bounded_int(
                ctx.get_config("refresh_token_ttl_days", 90), 90, 1, 365
            ),
            pairing_ttl_seconds=_bounded_int(
                ctx.get_config("pairing_ttl_seconds", 600), 600, 60, 3600
            ),
            max_file_bytes=_bounded_int(
                ctx.get_config("max_file_bytes", 50 * 1024 * 1024),
                50 * 1024 * 1024,
                1024,
                100 * 1024 * 1024,
            ),
            max_attachments_per_turn=_bounded_int(
                ctx.get_config("max_attachments_per_turn", 10), 10, 1, 20
            ),
            event_retention_hours=_bounded_int(
                ctx.get_config("run_event_retention_hours", 24), 24, 1, 168
            ),
            terminal_event_retention_days=_bounded_int(
                ctx.get_config("terminal_event_retention_days", 7), 7, 1, 30
            ),
            push=PushConfig(
                enabled=bool(push.get("enabled", True)),
                endpoint=str(
                    push.get("endpoint") or "https://exp.host/--/api/v2/push/send"
                ),
                timeout_seconds=_bounded_int(
                    push.get("timeout_seconds", 10), 10, 1, 30
                ),
                max_attempts=_bounded_int(push.get("max_attempts", 8), 8, 1, 20),
            ),
            files_enabled=bool(files.get("enabled", True)),
            ocr_enabled=bool(files.get("ocr_enabled", False)),
            quick_enabled=bool(quick.get("enabled", True)),
            quick_toolsets=quick_toolsets,
            quick_max_iterations=_bounded_int(
                quick.get("max_iterations", 8), 8, 1, 50
            ),
            quick_timeout_seconds=_bounded_int(
                quick.get("timeout_seconds", 180), 180, 10, 900
            ),
        )

    @property
    def plugin_root(self) -> Path:
        return self.default_home / "plugin-data" / "hermes-mobile"

    @property
    def control_db(self) -> Path:
        return self.plugin_root / "control.db"

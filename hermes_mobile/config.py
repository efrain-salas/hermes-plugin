from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APNS_ENVIRONMENTS = ("sandbox", "production")


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, parsed))


class PushConfigError(RuntimeError):
    """Raised when push is enabled with an unusable APNs configuration."""


@dataclass(frozen=True)
class APNsCredentials:
    """Resolved, non-loggable APNs token-auth material."""

    team_id: str
    key_id: str
    topic: str
    private_key_pem: str = field(repr=False)
    environments: tuple[str, ...] = APNS_ENVIRONMENTS


@dataclass(frozen=True)
class PushConfig:
    enabled: bool = False
    provider: str = "apns"
    topic: str = "app.hermes.mobile"
    team_id: str = ""
    key_id: str = ""
    private_key: str = field(default="", repr=False)
    key_path: str = ""
    environments: tuple[str, ...] = APNS_ENVIRONMENTS
    timeout_seconds: int = 10
    max_attempts: int = 8
    jwt_ttl_seconds: int = 3300
    max_payload_bytes: int = 4096
    endpoint_override: str = ""
    retry_base_seconds: int = 2
    http2: bool = True

    def configuration_error(self) -> str | None:
        """Return a clear diagnostic, or ``None`` when APNs is usable."""
        if not self.enabled:
            return None
        if self.provider != "apns":
            return f"push provider '{self.provider}' is no longer supported; use 'apns'"
        missing = [
            name for name in ("team_id", "key_id", "topic") if not getattr(self, name)
        ]
        if not self.private_key and not self.key_path:
            missing.append("private_key")
        elif self.key_path and not Path(self.key_path).is_file():
            return f"APNs key file not found: {Path(self.key_path).name}"
        if missing:
            return "missing APNs configuration: " + ", ".join(missing)
        if not self.environments:
            return "no APNs environment is enabled"
        return None

    def credentials(self) -> APNsCredentials:
        """Load and validate the private key, never exposing it in messages."""
        error = self.configuration_error()
        if error:
            raise PushConfigError(error)
        pem = self.private_key
        if not pem and self.key_path:
            try:
                pem = Path(self.key_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise PushConfigError(
                    f"could not read APNs key file: {exc.strerror or 'io_error'}"
                ) from exc
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key,
            )

            parsed = load_pem_private_key(pem.encode("utf-8"), password=None)
            if not isinstance(parsed, ec.EllipticCurvePrivateKey):
                raise PushConfigError("APNs private key must be an EC P-256 key")
        except PushConfigError:
            raise
        except Exception as exc:
            raise PushConfigError(
                "APNs private key is not a valid .p8 PEM file"
            ) from exc
        return APNsCredentials(
            team_id=self.team_id,
            key_id=self.key_id,
            topic=self.topic,
            private_key_pem=pem,
            environments=self.environments,
        )

    def validate(self) -> None:
        self.credentials()


def _normalize_environments(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return APNS_ENVIRONMENTS
    normalized = []
    for item in value:
        name = str(item).strip().lower()
        if name in APNS_ENVIRONMENTS and name not in normalized:
            normalized.append(name)
    return tuple(normalized)


def _push_config(push: Mapping[str, Any]) -> PushConfig:
    def _value(key: str, env: str) -> str:
        raw = push.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return os.environ.get(env, "").strip()
        return str(raw).strip()

    enabled = push.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"false", "0", "no", ""}
    return PushConfig(
        enabled=bool(enabled),
        provider=_value("provider", "HERMES_APNS_PROVIDER") or "apns",
        topic=_value("topic", "HERMES_APNS_TOPIC") or "app.hermes.mobile",
        team_id=_value("team_id", "HERMES_APNS_TEAM_ID"),
        key_id=_value("key_id", "HERMES_APNS_KEY_ID"),
        private_key=_value("private_key", "HERMES_APNS_PRIVATE_KEY"),
        key_path=_value("key_path", "HERMES_APNS_KEY_PATH"),
        environments=_normalize_environments(
            push.get("environments", os.environ.get("HERMES_APNS_ENVIRONMENTS"))
        ),
        timeout_seconds=_bounded_int(push.get("timeout_seconds", 10), 10, 1, 30),
        max_attempts=_bounded_int(push.get("max_attempts", 8), 8, 1, 20),
        jwt_ttl_seconds=_bounded_int(
            push.get("jwt_ttl_seconds", 3300), 3300, 60, 3600
        ),
        max_payload_bytes=_bounded_int(
            push.get("max_payload_bytes", 4096), 4096, 512, 4096
        ),
        endpoint_override=str(push.get("endpoint_override") or "").strip(),
        retry_base_seconds=_bounded_int(
            push.get("retry_base_seconds", 2), 2, 1, 60
        ),
        http2=bool(push.get("http2", True)),
    )





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
    quick_toolsets: tuple[str, ...] = ("search", "web")
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
        quick_toolsets = quick.get("toolsets", ("search", "web"))
        if isinstance(quick_toolsets, str):
            quick_toolsets = (quick_toolsets,)
        if not isinstance(quick_toolsets, (list, tuple)):
            quick_toolsets = ("search", "web")
        quick_toolsets = tuple(
            str(name).strip() for name in quick_toolsets if str(name).strip()
        ) or ("search", "web")
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
            push=_push_config(push),
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

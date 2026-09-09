from __future__ import annotations

import logging

from .api.routes import MobileAPI
from .cli import command, setup_parser
from .config import MobileConfig
from .files.tool import SCHEMA, read_attachment
from .runtime import MobileRuntime

logger = logging.getLogger("hermes_mobile")


def _session_end(**_: object) -> None:
    """Best-effort observation hook; durable reconciliation is owned by MobileRuntime."""
    return


def register(ctx) -> None:
    config = MobileConfig.from_context(ctx)
    runtime = MobileRuntime(config)
    api = MobileAPI(runtime)

    def _wire(app, adapter) -> None:
        try:
            api.wire(app, adapter)
        except Exception:
            logger.exception(
                "Hermes Mobile route registration failed; gateway will continue"
            )

    ctx.register_platform_handler("api_server", _wire)
    ctx.register_tool(
        name="mobile_attachment_read",
        toolset="hermes_mobile",
        schema=SCHEMA,
        handler=read_attachment,
        emoji="📎",
    )
    ctx.register_hook("on_session_end", _session_end)
    ctx.register_cli_command(
        name="mobile",
        help="Hermes Mobile provisioning and device management",
        setup_fn=setup_parser,
        handler_fn=command,
        description="Provision, diagnose, pair and revoke Hermes Mobile devices.",
    )
    ctx.register_redaction_patterns(
        [
            r"ExponentPushToken\[[A-Za-z0-9_-]{8,}\]",
            r"ExpoPushToken\[[A-Za-z0-9_-]{8,}\]",
            r"hermes://pair\?[^\s]+",
        ]
    )

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import aiohttp

from ..api.errors import MobileError
from ..constants import REASONING_EFFORTS


class HermesAPIClient:
    """Stable loopback adapter. All Hermes wire details stay in this module."""

    def __init__(
        self, base_url: str, *, key_provider: Callable[[str], str] | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.key_provider = key_provider
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60, sock_read=300)
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    @contextmanager
    def _secret_scope(self, profile: str) -> Iterator[str]:
        if self.key_provider:
            yield self.key_provider(profile)
            return
        token = None
        try:
            from agent.secret_scope import (
                build_profile_secret_scope,
                get_secret,
                reset_secret_scope,
                set_secret_scope,
            )
            from hermes_cli.profiles import get_profile_dir

            try:
                key = get_secret("API_SERVER_KEY", "") or ""
            except Exception:
                token = set_secret_scope(
                    build_profile_secret_scope(get_profile_dir(profile))
                )
                key = get_secret("API_SERVER_KEY", "") or ""
            if not key:
                raise MobileError(
                    "gateway_unavailable",
                    "Hermes API authentication is not configured.",
                    503,
                    retryable=True,
                )
            yield key
        finally:
            if token is not None:
                reset_secret_scope(token)

    def _url(self, profile: str, path: str) -> str:
        return f"{self.base_url}/p/{quote(profile, safe='')}{path}"

    async def _request(
        self,
        method: str,
        profile: str,
        path: str,
        *,
        expected: set[int] | None = None,
        **kwargs: Any,
    ) -> tuple[int, Any, dict[str, str]]:
        await self.start()
        with self._secret_scope(profile) as key:
            headers = {"Authorization": f"Bearer {key}", **kwargs.pop("headers", {})}
            try:
                assert self.session is not None
                async with self.session.request(
                    method, self._url(profile, path), headers=headers, **kwargs
                ) as response:
                    data = (
                        await response.json(content_type=None)
                        if response.content_length != 0
                        else None
                    )
                    if expected is not None and response.status not in expected:
                        remote = data.get("error", {}) if isinstance(data, dict) else {}
                        code = remote.get("code") or "gateway_unavailable"
                        status = (
                            404
                            if response.status == 404
                            else 409
                            if response.status == 409
                            else 503
                            if response.status >= 500
                            else response.status
                        )
                        raise MobileError(
                            code,
                            "Hermes no pudo completar la operación.",
                            status,
                            retryable=response.status >= 500,
                        )
                    return response.status, data, dict(response.headers)
            except MobileError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise MobileError(
                    "gateway_unavailable",
                    "Hermes Gateway no está disponible.",
                    503,
                    retryable=True,
                ) from exc

    async def capabilities(self, profile: str) -> dict[str, Any]:
        return (
            await self._request("GET", profile, "/v1/capabilities", expected={200})
        )[1]

    async def list_conversations(self, profile: str, **params: Any) -> dict[str, Any]:
        return (
            await self._request(
                "GET", profile, "/api/sessions", params=params, expected={200}
            )
        )[1]

    async def create_conversation(
        self, profile: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST", profile, "/api/sessions", json=body, expected={201}
            )
        )[1]

    async def get_conversation(self, profile: str, session_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                profile,
                f"/api/sessions/{quote(session_id, safe='')}",
                expected={200},
            )
        )[1]

    async def update_conversation(
        self, profile: str, session_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            await self._request(
                "PATCH",
                profile,
                f"/api/sessions/{quote(session_id, safe='')}",
                json=body,
                expected={200},
            )
        )[1]

    async def set_conversation_model(
        self,
        profile: str,
        session_id: str,
        model: str,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        reasoning: dict[str, Any] = {"enabled": reasoning_effort != "none"}
        if reasoning_effort not in {None, "none"}:
            reasoning["effort"] = reasoning_effort
        return (
            await self._request(
                "POST",
                profile,
                f"/api/sessions/{quote(session_id, safe='')}/model",
                json={
                    "model": model,
                    "model_options": (
                        {"reasoning": reasoning} if reasoning_effort else {}
                    ),
                },
                expected={200},
            )
        )[1]

    async def delete_conversation(self, profile: str, session_id: str) -> None:
        await self._request(
            "DELETE",
            profile,
            f"/api/sessions/{quote(session_id, safe='')}",
            expected={200, 204},
        )

    async def get_messages(
        self, profile: str, session_id: str, **params: Any
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                profile,
                f"/api/sessions/{quote(session_id, safe='')}/messages",
                params=params,
                expected={200},
            )
        )[1]

    async def fork_conversation(
        self, profile: str, session_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/api/sessions/{quote(session_id, safe='')}/fork",
                json=body,
                expected={201},
            )
        )[1]

    async def create_run(
        self, profile: str, body: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                "/v1/runs",
                json=body,
                headers={"Idempotency-Key": idempotency_key},
                expected={200, 202},
            )
        )[1]

    async def get_run(self, profile: str, run_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "GET", profile, f"/v1/runs/{quote(run_id, safe='')}", expected={200}
            )
        )[1]

    async def stream_run_events(
        self, profile: str, run_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        await self.start()
        with self._secret_scope(profile) as key:
            assert self.session is not None
            try:
                async with self.session.get(
                    self._url(profile, f"/v1/runs/{quote(run_id, safe='')}/events"),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Accept": "text/event-stream",
                    },
                ) as response:
                    if response.status != 200:
                        raise MobileError(
                            "gateway_unavailable",
                            "No se pudo abrir el stream de Hermes.",
                            503,
                            retryable=True,
                        )
                    data_lines: list[str] = []
                    async for raw in response.content:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                yield json.loads("\n".join(data_lines))
                                data_lines.clear()
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
            except MobileError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise MobileError(
                    "gateway_unavailable",
                    "El stream de Hermes se interrumpió.",
                    503,
                    retryable=True,
                ) from exc

    async def cancel_run(self, profile: str, run_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/v1/runs/{quote(run_id, safe='')}/stop",
                json={},
                expected={200},
            )
        )[1]

    async def steer_run(
        self, profile: str, run_id: str, instruction: str
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/v1/runs/{quote(run_id, safe='')}/steer",
                json={"input": instruction},
                expected={200, 202},
            )
        )[1]

    async def answer_approval(
        self, profile: str, run_id: str, request_id: str, choice: str
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/v1/runs/{quote(run_id, safe='')}/approval",
                json={"request_id": request_id, "choice": choice},
                expected={200},
            )
        )[1]

    async def list_scheduled_tasks(
        self, profile: str, *, include_disabled: bool = True
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                profile,
                "/api/jobs",
                params={"include_disabled": str(include_disabled).lower()},
                expected={200},
            )
        )[1]

    async def get_scheduled_task(self, profile: str, job_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "GET", profile, f"/api/jobs/{quote(job_id, safe='')}", expected={200}
            )
        )[1]

    async def update_scheduled_task(
        self, profile: str, job_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            await self._request(
                "PATCH",
                profile,
                f"/api/jobs/{quote(job_id, safe='')}",
                json=body,
                expected={200},
            )
        )[1]

    async def delete_scheduled_task(self, profile: str, job_id: str) -> None:
        await self._request(
            "DELETE", profile, f"/api/jobs/{quote(job_id, safe='')}", expected={200}
        )

    async def pause_scheduled_task(self, profile: str, job_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/api/jobs/{quote(job_id, safe='')}/pause",
                json={},
                expected={200},
            )
        )[1]

    async def resume_scheduled_task(self, profile: str, job_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/api/jobs/{quote(job_id, safe='')}/resume",
                json={},
                expected={200},
            )
        )[1]

    async def run_scheduled_task(self, profile: str, job_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                profile,
                f"/api/jobs/{quote(job_id, safe='')}/run",
                json={},
                expected={200},
            )
        )[1]

    async def models(self, profile: str) -> dict[str, Any]:
        options = (
            await self._request("GET", profile, "/api/model/options", expected={200})
        )[1]
        if not isinstance(options, dict):
            options = {}

        provider = str(options.get("provider") or "").strip()
        default_model = str(options.get("model") or "").strip()
        provider_models: list[str] = []
        providers = options.get("providers")
        if isinstance(providers, list):
            current = next(
                (
                    item
                    for item in providers
                    if isinstance(item, dict)
                    and str(item.get("slug") or "").strip() == provider
                ),
                None,
            )
            if current is None:
                current = next(
                    (
                        item
                        for item in providers
                        if isinstance(item, dict) and item.get("is_current") is True
                    ),
                    None,
                )
            if current:
                raw_models = current.get("models")
                if not isinstance(raw_models, list):
                    raw_models = []
                raw_capabilities = current.get("capabilities")
                if not isinstance(raw_capabilities, dict):
                    raw_capabilities = {}
                provider_models = [
                    model.strip()
                    for model in raw_models
                    if isinstance(model, str) and model.strip()
                ]
            else:
                raw_capabilities = {}
        else:
            raw_capabilities = {}

        # The configured model remains selectable even if a live/curated
        # provider catalog is temporarily empty or stale.
        if default_model and default_model not in provider_models:
            provider_models.insert(0, default_model)

        return {
            "data": [
                self._model_resource(model, provider, raw_capabilities)
                for model in dict.fromkeys(provider_models)
            ],
            "default": default_model or None,
            "provider": provider or None,
        }

    @staticmethod
    def _model_resource(
        model: str, provider: str, capabilities: dict[str, Any]
    ) -> dict[str, Any]:
        raw = capabilities.get(model)
        raw = raw if isinstance(raw, dict) else {}
        supports_reasoning = raw.get("reasoning") is not False
        can_disable = raw.get("can_disable_reasoning")
        can_disable = can_disable if isinstance(can_disable, bool) else None
        efforts = list(REASONING_EFFORTS) if supports_reasoning else []
        if can_disable is False:
            efforts.remove("none")
        return {
            "id": model,
            "object": "model",
            "owned_by": provider,
            "reasoning": {
                "supported": supports_reasoning,
                "can_disable": can_disable,
                "efforts": efforts,
            },
        }

    async def toolsets(self, profile: str) -> dict[str, Any]:
        return (await self._request("GET", profile, "/v1/toolsets", expected={200}))[1]

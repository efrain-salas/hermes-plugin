from __future__ import annotations

from typing import Any

import aiohttp


class ExpoTemporaryError(RuntimeError):
    pass


class ExpoPermanentError(RuntimeError):
    pass


async def send_expo(
    session: aiohttp.ClientSession,
    endpoint: str,
    token: str,
    payload: dict[str, Any],
    timeout: int,
) -> str | None:
    message = {"to": token, "sound": "default", **payload}
    try:
        async with session.post(
            endpoint, json=message, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            if response.status == 429 or response.status >= 500:
                raise ExpoTemporaryError(f"http_{response.status}")
            if response.status >= 400:
                raise ExpoPermanentError(f"http_{response.status}")
            body = await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise ExpoTemporaryError("network_error") from exc
    item = body.get("data") if isinstance(body, dict) else None
    if isinstance(item, list):
        item = item[0] if item else {}
    if not isinstance(item, dict):
        raise ExpoTemporaryError("invalid_response")
    if item.get("status") == "error":
        code = str((item.get("details") or {}).get("error") or "provider_error")
        if code == "DeviceNotRegistered":
            raise ExpoPermanentError(code)
        raise ExpoTemporaryError(code)
    return str(item.get("id")) if item.get("id") else None


async def check_expo_receipt(
    session: aiohttp.ClientSession, endpoint: str, ticket: str, timeout: int
) -> None:
    receipt_endpoint = endpoint.rsplit("/", 1)[0] + "/getReceipts"
    try:
        async with session.post(
            receipt_endpoint,
            json={"ids": [ticket]},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status == 429 or response.status >= 500:
                raise ExpoTemporaryError(f"receipt_http_{response.status}")
            if response.status >= 400:
                raise ExpoPermanentError(f"receipt_http_{response.status}")
            body = await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise ExpoTemporaryError("receipt_network_error") from exc
    data = body.get("data") if isinstance(body, dict) else None
    item = data.get(ticket) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise ExpoTemporaryError("receipt_pending")
    if item.get("status") == "error":
        code = str((item.get("details") or {}).get("error") or "receipt_error")
        if code == "DeviceNotRegistered":
            raise ExpoPermanentError(code)
        raise ExpoTemporaryError(code)

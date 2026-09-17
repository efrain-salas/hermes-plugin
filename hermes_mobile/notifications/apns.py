from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import APNsCredentials
from .plaintext import plain_notification_text

APNS_HOSTS = {
    "sandbox": "api.sandbox.push.apple.com",
    "production": "api.push.apple.com",
}

APNS_PUSH_TYPE = "alert"
APNS_PRIORITY = "10"
APNS_EXPIRATION_SECONDS = 3600
MAX_PAYLOAD_BYTES = 4096


class APNsError(RuntimeError):
    """Base class for a definitive APNs response classification."""

    def __init__(self, reason: str, *, status: int | None = None, apns_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.apns_id = apns_id


class APNsTemporaryError(APNsError):
    """Transient failure: the same request may succeed later."""


class APNsPermanentError(APNsError):
    """Definitive failure that must not be retried nor revoke devices."""


class APNsUnregisteredError(APNsPermanentError):
    """APNs reports the concrete token is no longer valid."""


def apns_url(environment: str, device_token: str, endpoint_override: str = "") -> str:
    if endpoint_override:
        if "{token}" in endpoint_override:
            return endpoint_override.replace("{token}", device_token)
        return f"{endpoint_override.rstrip('/')}/{device_token}"
    host = APNS_HOSTS.get(environment)
    if not host:
        raise APNsPermanentError(f"unsupported_environment:{environment or 'missing'}")
    return f"https://{host}/3/device/{device_token}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encode_apns_jwt(credentials: APNsCredentials, issued_at: int) -> str:
    """Build a short-lived ES256 provider token (``kid``/``iss``/``iat``)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
    )

    private_key = serialization.load_pem_private_key(
        credentials.private_key_pem.encode("utf-8"), password=None
    )
    header = {"alg": "ES256", "kid": credentials.key_id}
    claims = {"iss": credentials.team_id, "iat": int(issued_at)}
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
        f"{_b64url(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}"
    )
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{_b64url(raw)}"


class APNsTokenProvider:
    """Cache one provider token and renew it well before Apple's one-hour cap."""

    def __init__(self, credentials: APNsCredentials, ttl_seconds: int = 3300):
        self.credentials = credentials
        self.ttl_seconds = min(3600, max(60, int(ttl_seconds)))
        self._token: str | None = None
        self._issued_at = 0

    def token(self, now: float | None = None) -> str:
        moment = int(now if now is not None else time.time())
        if self._token is None or moment - self._issued_at >= self.ttl_seconds:
            self._token = encode_apns_jwt(self.credentials, moment)
            self._issued_at = moment
        return self._token


def _dump(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")


def build_payload(
    message: dict[str, Any], max_bytes: int = MAX_PAYLOAD_BYTES
) -> bytes:
    """Translate the internal notification into an ``aps``-shaped payload."""
    title = plain_notification_text(message.get("title"))
    body = plain_notification_text(message.get("body"))
    custom: dict[str, Any] = {}
    data = message.get("data")
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "aps" or value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                custom[str(key)] = value
    alert: dict[str, Any] = {"title": title, "body": body}
    payload: dict[str, Any] = {"aps": {"alert": alert, "sound": "default"}, **custom}
    raw = _dump(payload)
    if len(raw) <= max_bytes:
        return raw

    # Preserve navigation identifiers: trim the human-readable body first.
    for _ in range(32):
        if len(raw) <= max_bytes or not alert["body"]:
            break
        overflow = len(raw) - max_bytes
        alert["body"] = alert["body"][: max(0, len(alert["body"]) - overflow - 1)]
        alert["body"] = f"{alert['body']}…" if alert["body"] else ""
        raw = _dump(payload)
    if len(raw) > max_bytes:
        payload["aps"]["alert"] = title or "Hermes"
        raw = _dump(payload)
    # Last resort: an oversized title still must respect Apple's 4 KB cap.
    for _ in range(32):
        if len(raw) <= max_bytes:
            break
        text = payload["aps"]["alert"]
        if not isinstance(text, str) or not text:
            break
        overflow = len(raw) - max_bytes
        payload["aps"]["alert"] = text[: max(0, len(text) - overflow - 1)]
        raw = _dump(payload)
    return raw


def classify_response(
    status: int, reason: str | None, apns_id: str | None = None
) -> APNsError | None:
    """Map an APNs HTTP status/reason to a retry policy."""
    if 200 <= status < 300:
        return None
    if status == 429 or status >= 500:
        return APNsTemporaryError(reason or f"http_{status}", status=status, apns_id=apns_id)
    if status == 410:
        return APNsUnregisteredError(
            reason or "Unregistered", status=status, apns_id=apns_id
        )
    if status == 403 and reason == "ExpiredProviderToken":
        return APNsTemporaryError(reason, status=status, apns_id=apns_id)
    return APNsPermanentError(reason or f"http_{status}", status=status, apns_id=apns_id)


@dataclass(frozen=True)
class APNsResult:
    apns_id: str | None


def create_apns_client(
    timeout_seconds: int = 10, http2: bool = True
) -> httpx.AsyncClient:
    """Build the shared HTTP/2 TLS client used to reach APNs.

    ``http2`` can be disabled only for local cleartext test servers; APNs
    itself always requires HTTP/2.
    """
    return httpx.AsyncClient(http2=http2, timeout=httpx.Timeout(timeout_seconds))


async def send_apns(
    client: httpx.AsyncClient,
    credentials: APNsCredentials,
    token_provider: APNsTokenProvider,
    device_token: str,
    environment: str,
    payload: bytes,
    *,
    timeout_seconds: int = 10,
    endpoint_override: str = "",
    now: float | None = None,
) -> APNsResult:
    url = apns_url(environment, device_token, endpoint_override)
    moment = time.time() if now is None else now
    headers = {
        "authorization": f"bearer {token_provider.token(moment)}",
        "apns-topic": credentials.topic,
        "apns-push-type": APNS_PUSH_TYPE,
        "apns-priority": APNS_PRIORITY,
        "apns-expiration": str(int(moment) + APNS_EXPIRATION_SECONDS),
        "content-type": "application/json",
    }
    try:
        response = await client.post(
            url, content=payload, headers=headers, timeout=timeout_seconds
        )
    except httpx.HTTPError as exc:
        raise APNsTemporaryError("network_error") from exc
    apns_id = response.headers.get("apns-id")
    reason: str | None = None
    if response.status_code >= 400:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("reason"):
            reason = str(parsed["reason"])
    error = classify_response(response.status_code, reason, apns_id)
    if error is not None:
        raise error
    return APNsResult(apns_id=apns_id)

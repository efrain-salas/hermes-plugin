from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..constants import API_VERSION, PLUGIN_ID
from ..ids import new_id


class TokenError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class TokenManager:
    def __init__(self, key_dir: Path, ttl_seconds: int, key_id: str = "mobile-v1"):
        self.key_dir = Path(key_dir)
        self.ttl_seconds = ttl_seconds
        self.key_id = os.environ.get("HERMES_MOBILE_JWT_KEY_ID", key_id)
        self.private_path = self.key_dir / "jwt-ed25519.pem"
        self._private = self._load_or_create()
        self._public = self._private.public_key()

    def _load_or_create(self) -> Ed25519PrivateKey:
        configured = os.environ.get("HERMES_MOBILE_JWT_PRIVATE_KEY", "").strip()
        if configured:
            source = (
                configured.encode()
                if "BEGIN PRIVATE KEY" in configured
                else Path(configured).read_bytes()
            )
            return serialization.load_pem_private_key(source, password=None)  # type: ignore[return-value]
        try:
            raw = self.private_path.read_bytes()
            return serialization.load_pem_private_key(raw, password=None)  # type: ignore[return-value]
        except FileNotFoundError:
            private = Ed25519PrivateKey.generate()
            pem = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            try:
                _secure_write(self.private_path, pem)
            except FileExistsError:
                return serialization.load_pem_private_key(
                    self.private_path.read_bytes(), password=None
                )  # type: ignore[return-value]
            return private

    def issue(
        self,
        *,
        user_id: str,
        device_id: str,
        profile: str,
        scopes: list[str] | tuple[str, ...],
    ) -> tuple[str, int]:
        now = int(time.time())
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self.key_id}
        payload = {
            "iss": PLUGIN_ID,
            "aud": f"hermes-mobile:{API_VERSION}",
            "sub": user_id,
            "device_id": device_id,
            "profile": profile,
            "scopes": list(scopes),
            "iat": now,
            "exp": now + self.ttl_seconds,
            "jti": new_id("tok"),
        }
        signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
        signature = self._private.sign(signing_input.encode("ascii"))
        return f"{signing_input}.{_b64(signature)}", self.ttl_seconds

    def verify(self, token: str, profile: str) -> dict[str, Any]:
        try:
            h64, p64, s64 = token.split(".")
            if any(_b64(_unb64(part)) != part for part in (h64, p64, s64)):
                raise TokenError("invalid_token")
            header = json.loads(_unb64(h64))
            payload = json.loads(_unb64(p64))
            if header != {"alg": "EdDSA", "typ": "JWT", "kid": self.key_id}:
                raise TokenError("invalid_token")
            self._public.verify(_unb64(s64), f"{h64}.{p64}".encode("ascii"))
        except TokenError:
            raise
        except Exception as exc:
            raise TokenError("invalid_token") from exc
        if (
            payload.get("iss") != PLUGIN_ID
            or payload.get("aud") != f"hermes-mobile:{API_VERSION}"
        ):
            raise TokenError("invalid_token")
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise TokenError("token_expired")
        if payload.get("profile") != profile:
            raise TokenError("profile_mismatch")
        if (
            not payload.get("sub")
            or not payload.get("device_id")
            or not isinstance(payload.get("scopes"), list)
        ):
            raise TokenError("invalid_token")
        return payload

    def public_jwk(self) -> dict[str, str]:
        raw = self._public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _b64(raw),
            "kid": self.key_id,
            "alg": "EdDSA",
            "use": "sig",
        }


class SecretBox:
    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        try:
            self.key = self.key_path.read_bytes()
        except FileNotFoundError:
            self.key = AESGCM.generate_key(bit_length=256)
            try:
                _secure_write(self.key_path, self.key)
            except FileExistsError:
                self.key = self.key_path.read_bytes()
        if len(self.key) != 32:
            raise ValueError("invalid encryption key")

    def encrypt(self, value: str) -> str:
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.key).encrypt(
            nonce, value.encode("utf-8"), PLUGIN_ID.encode()
        )
        return _b64(nonce + encrypted)

    def decrypt(self, value: str) -> str:
        raw = _unb64(value)
        return (
            AESGCM(self.key)
            .decrypt(raw[:12], raw[12:], PLUGIN_ID.encode())
            .decode("utf-8")
        )

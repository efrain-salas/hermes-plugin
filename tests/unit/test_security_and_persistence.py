from __future__ import annotations

import json

import pytest

from hermes_mobile.persistence.repositories import (
    ControlStore,
    InvalidPairing,
    RefreshReuse,
)
from hermes_mobile.security.tokens import SecretBox, TokenError, TokenManager


def test_access_tokens_are_profile_bound_and_tamper_evident(tmp_path):
    manager = TokenManager(tmp_path / "keys", 900)
    token, ttl = manager.issue(
        user_id="usr_12345678",
        device_id="dev_12345678",
        profile="default",
        scopes=["x"],
    )
    assert ttl == 900
    assert manager.verify(token, "default")["device_id"] == "dev_12345678"
    with pytest.raises(TokenError, match="profile_mismatch"):
        manager.verify(token, "mujer")
    with pytest.raises(TokenError, match="invalid_token"):
        manager.verify(token[:-1] + ("A" if token[-1] != "A" else "B"), "default")
    assert (tmp_path / "keys" / "jwt-ed25519.pem").stat().st_mode & 0o777 == 0o600
    assert manager.public_jwk() == {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": manager.public_jwk()["x"],
        "kid": "mobile-v1",
        "alg": "EdDSA",
        "use": "sig",
    }


def test_secret_box_roundtrip_and_no_plaintext(tmp_path):
    box = SecretBox(tmp_path / "data.key")
    encrypted = box.encrypt("ExponentPushToken[secret-value]")
    assert "secret-value" not in encrypted
    assert box.decrypt(encrypted) == "ExponentPushToken[secret-value]"


def test_pairing_single_use_refresh_rotation_and_reuse_revocation(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    pairing = store.create_pairing("default", "Alice", 600)
    device = {"installation_id": "install-123", "name": "Phone", "platform": "ios"}
    paired = store.consume_pairing(
        "default", pairing["token"], device, ("devices:self",)
    )
    with pytest.raises(InvalidPairing):
        store.consume_pairing("default", pairing["token"], device, ("devices:self",))
    refresh = store.issue_refresh(paired["device"]["id"], 90)
    row, replacement = store.rotate_refresh(refresh, 90)
    assert row["profile_id"] == "default" and replacement != refresh
    with pytest.raises(RefreshReuse):
        store.rotate_refresh(refresh, 90)
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NOT NULL"
            ).fetchone()[0]
            == 2
        )


def test_outbox_is_deduplicated_and_respects_preferences(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    pairing = store.create_pairing("default", "Alice", 600)
    paired = store.consume_pairing(
        "default",
        pairing["token"],
        {"installation_id": "install-123", "name": "Phone", "platform": "ios"},
        ("devices:self",),
    )
    store.update_device(
        paired["device"]["id"],
        {
            "push_token_encrypted": "encrypted",
            "notification_preferences_json": json.dumps({"turn_completed": True}),
        },
    )
    assert (
        store.enqueue_push("default", "run.completed", "run_x", {"title": "Hermes"})
        == 1
    )
    assert (
        store.enqueue_push("default", "run.completed", "run_x", {"title": "Hermes"})
        == 0
    )

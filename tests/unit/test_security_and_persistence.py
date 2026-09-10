from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_mobile.persistence.repositories import (
    ControlStore,
    InvalidPairing,
    ProfileStore,
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


def test_profile_store_migrates_and_persists_conversation_reasoning(tmp_path):
    root = tmp_path / "plugin-data" / "hermes-mobile"
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "profile.db")
    db.executescript(
        """
        CREATE TABLE conversation_map (
            public_id TEXT PRIMARY KEY,
            hermes_session_id TEXT NOT NULL UNIQUE,
            title_override TEXT,
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            last_read_message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        );
        INSERT INTO conversation_map (
            public_id, hermes_session_id, created_at, updated_at
        ) VALUES ('conv_existing', 'session-existing', 'now', 'now');
        """
    )
    db.close()

    store = ProfileStore(tmp_path)
    store.initialize()
    with store.connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(conversation_map)")
        }
    assert "reasoning_effort" in columns
    assert store.conversation("conv_existing")["reasoning_effort"] is None
    store.update_conversation("conv_existing", {"reasoning_effort": "xhigh"})
    assert store.conversation("conv_existing")["reasoning_effort"] == "xhigh"


def test_deleting_scheduled_task_removes_runs_and_keeps_tombstone(tmp_path):
    store = ProfileStore(tmp_path)
    store.initialize()
    task = store.ensure_scheduled_task("native-job")
    run = store.ensure_scheduled_run(
        task["public_id"], "native-execution", "2026-09-10T08:00:00Z"
    )

    deleted = store.delete_scheduled_task(task["public_id"])

    assert deleted is not None and deleted["deleted_at"]
    assert store.scheduled_task(task["public_id"]) is None
    assert store.scheduled_run(run["public_id"]) is None
    with store.connect() as connection:
        journal = connection.execute(
            "SELECT operation,payload_json FROM sync_journal "
            "WHERE entity_type='scheduled_task' AND entity_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (task["public_id"],),
        ).fetchone()
    assert journal["operation"] == "deleted"
    assert json.loads(journal["payload_json"])["deleted_at"] == deleted["deleted_at"]


def test_inbox_is_durable_deduplicated_filterable_and_journaled(tmp_path):
    store = ProfileStore(tmp_path)
    store.initialize()
    unread, created = store.create_inbox_item(
        kind="gateway.restarted",
        severity="info",
        title="Gateway reiniciado",
        body="Hermes vuelve a estar disponible.",
        source_type="gateway",
        source_id="boot-1",
        dedupe_key="gateway.restarted:boot-1",
    )
    replay, replay_created = store.create_inbox_item(
        kind="gateway.restarted",
        severity="info",
        title="Duplicado",
        body="No debe insertarse.",
        source_type="gateway",
        source_id="boot-1",
        dedupe_key="gateway.restarted:boot-1",
    )
    assert created is True and replay_created is False
    assert replay["public_id"] == unread["public_id"]

    store.mark_inbox_read(unread["public_id"])
    read_rows, unread_count = store.list_inbox(
        limit=10, offset=0, unread=False, kind="gateway"
    )
    unread_rows, _ = store.list_inbox(
        limit=10, offset=0, unread=True, kind="gateway"
    )
    assert [row["public_id"] for row in read_rows] == [unread["public_id"]]
    assert unread_rows == [] and unread_count == 0

    with store.connect() as connection:
        operations = connection.execute(
            "SELECT operation FROM sync_journal "
            "WHERE entity_type='inbox_item' AND entity_id=? ORDER BY sequence",
            (unread["public_id"],),
        ).fetchall()
    assert [row["operation"] for row in operations] == ["created", "updated"]

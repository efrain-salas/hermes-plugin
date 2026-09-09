from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..ids import new_id
from .migrations import (
    CONTROL_SCHEMA,
    CONTROL_SCHEMA_VERSION,
    PROFILE_SCHEMA,
    PROFILE_SCHEMA_VERSION,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StoreError(RuntimeError):
    pass


class InvalidPairing(StoreError):
    pass


class InvalidRefresh(StoreError):
    pass


class RefreshReuse(StoreError):
    pass


class SQLiteStore:
    def __init__(self, path: Path, schema: str, version: int):
        self.path = Path(path)
        self.schema = schema
        self.version = version

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self.connect() as conn:
            conn.executescript(self.schema)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (self.version, iso()),
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def transaction(self, callback: Callable[[sqlite3.Connection], Any]) -> Any:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = callback(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise


class ControlStore(SQLiteStore):
    def __init__(self, path: Path):
        super().__init__(path, CONTROL_SCHEMA, CONTROL_SCHEMA_VERSION)

    def create_pairing(
        self, profile: str, display_name: str, ttl_seconds: int
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        row = {
            "id": new_id("pair"),
            "profile_id": profile,
            "token_hash": secret_hash(token),
            "display_name": display_name,
            "expires_at": iso(now + timedelta(seconds=ttl_seconds)),
            "created_at": iso(now),
        }
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pairing_tokens(id,profile_id,token_hash,display_name,expires_at,created_at) "
                "VALUES (:id,:profile_id,:token_hash,:display_name,:expires_at,:created_at)",
                row,
            )
        return {**row, "token": token}

    def consume_pairing(
        self, profile: str, token: str, device: dict[str, Any], scopes: tuple[str, ...]
    ) -> dict[str, Any]:
        now = iso()

        def _consume(conn: sqlite3.Connection) -> dict[str, Any]:
            pair = conn.execute(
                "SELECT * FROM pairing_tokens WHERE token_hash=?", (secret_hash(token),)
            ).fetchone()
            if not pair:
                raise InvalidPairing("invalid")
            conn.execute(
                "UPDATE pairing_tokens SET attempt_count=attempt_count+1 WHERE id=?",
                (pair["id"],),
            )
            if (
                pair["profile_id"] != profile
                or pair["consumed_at"]
                or parse_time(pair["expires_at"]) <= utcnow()
            ):
                raise InvalidPairing("expired_or_consumed")
            conn.execute(
                "UPDATE pairing_tokens SET consumed_at=? WHERE id=?", (now, pair["id"])
            )
            user = conn.execute(
                "SELECT * FROM users WHERE profile_id=?", (profile,)
            ).fetchone()
            if not user:
                user_id = new_id("usr")
                conn.execute(
                    "INSERT INTO users(id,profile_id,display_name,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, profile, pair["display_name"], "active", now, now),
                )
                user = conn.execute(
                    "SELECT * FROM users WHERE id=?", (user_id,)
                ).fetchone()
            installation = str(device["installation_id"])
            existing = conn.execute(
                "SELECT * FROM devices WHERE user_id=? AND installation_id=?",
                (user["id"], installation),
            ).fetchone()
            device_id = existing["id"] if existing else new_id("dev")
            if existing:
                conn.execute(
                    "UPDATE devices SET name=?,platform=?,app_version=?,locale=?,timezone=?,"
                    "scopes_json=?,revoked_at=NULL,last_seen_at=?,updated_at=? WHERE id=?",
                    (
                        device["name"],
                        device["platform"],
                        device.get("app_version"),
                        device.get("locale"),
                        device.get("timezone"),
                        json_dump(scopes),
                        now,
                        now,
                        device_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO devices(id,user_id,installation_id,name,platform,app_version,locale,timezone,"
                    "notification_preferences_json,scopes_json,last_seen_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        device_id,
                        user["id"],
                        installation,
                        device["name"],
                        device["platform"],
                        device.get("app_version"),
                        device.get("locale"),
                        device.get("timezone"),
                        "{}",
                        json_dump(scopes),
                        now,
                        now,
                        now,
                    ),
                )
            return {
                "user": dict(user),
                "device": dict(
                    conn.execute(
                        "SELECT * FROM devices WHERE id=?", (device_id,)
                    ).fetchone()
                ),
            }

        return self.transaction(_consume)

    def issue_refresh(
        self, device_id: str, ttl_days: int, family_id: str | None = None
    ) -> str:
        raw = secrets.token_urlsafe(32)
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO refresh_tokens(id,device_id,family_id,token_hash,issued_at,expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    new_id("tok"),
                    device_id,
                    family_id or new_id("fam"),
                    secret_hash(raw),
                    iso(now),
                    iso(now + timedelta(days=ttl_days)),
                ),
            )
        return raw

    def rotate_refresh(self, raw: str, ttl_days: int) -> tuple[dict[str, Any], str]:
        token_hash = secret_hash(raw)
        new_raw = secrets.token_urlsafe(32)
        now = utcnow()

        def _rotate(conn: sqlite3.Connection) -> tuple[dict[str, Any] | None, str]:
            row = conn.execute(
                "SELECT r.*,d.user_id,d.revoked_at AS device_revoked,u.profile_id,u.display_name,u.status "
                "FROM refresh_tokens r JOIN devices d ON d.id=r.device_id "
                "JOIN users u ON u.id=d.user_id WHERE r.token_hash=?",
                (token_hash,),
            ).fetchone()
            if not row:
                raise InvalidRefresh("invalid")
            if row["rotated_at"]:
                conn.execute(
                    "UPDATE refresh_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE family_id=?",
                    (iso(now), row["family_id"]),
                )
                return None, "reused"
            if (
                row["revoked_at"]
                or row["device_revoked"]
                or row["status"] != "active"
                or parse_time(row["expires_at"]) <= now
            ):
                raise InvalidRefresh("expired_or_revoked")
            replacement_id = new_id("tok")
            conn.execute(
                "INSERT INTO refresh_tokens(id,device_id,family_id,token_hash,issued_at,expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    replacement_id,
                    row["device_id"],
                    row["family_id"],
                    secret_hash(new_raw),
                    iso(now),
                    iso(now + timedelta(days=ttl_days)),
                ),
            )
            conn.execute(
                "UPDATE refresh_tokens SET rotated_at=?,replaced_by_id=? WHERE id=?",
                (iso(now), replacement_id, row["id"]),
            )
            return dict(row), new_raw

        result = self.transaction(_rotate)
        if result[0] is None:
            raise RefreshReuse("reused")
        return result  # type: ignore[return-value]

    def auth_subject(
        self, user_id: str, device_id: str, profile: str
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT d.*,u.profile_id,u.display_name,u.status AS user_status FROM devices d "
                "JOIN users u ON u.id=d.user_id WHERE u.id=? AND d.id=? AND u.profile_id=?",
                (user_id, device_id, profile),
            ).fetchone()
            if not row or row["revoked_at"] or row["user_status"] != "active":
                return None
            return dict(row)

    def revoke_device(self, device_id: str) -> bool:
        now = iso()

        def _revoke(conn: sqlite3.Connection) -> bool:
            changed = conn.execute(
                "UPDATE devices SET revoked_at=?,push_token_encrypted=NULL,updated_at=? "
                "WHERE id=? AND revoked_at IS NULL",
                (now, now, device_id),
            ).rowcount
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE device_id=?",
                (now, device_id),
            )
            return bool(changed)

        return self.transaction(_revoke)

    def list_devices(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM devices WHERE user_id=? ORDER BY created_at",
                    (user_id,),
                )
            ]

    def update_device(
        self, device_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {
            "name",
            "platform",
            "app_version",
            "locale",
            "timezone",
            "push_provider",
            "push_token_encrypted",
            "notification_preferences_json",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if clean:
            clean["updated_at"] = iso()
            columns = ",".join(f"{key}=?" for key in clean)
            with self.connect() as conn:
                conn.execute(
                    f"UPDATE devices SET {columns} WHERE id=? AND revoked_at IS NULL",
                    (*clean.values(), device_id),
                )
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            return dict(row) if row else None

    def enqueue_push(
        self, profile: str, kind: str, dedupe_key: str, payload: dict[str, Any]
    ) -> int:
        now = iso()
        with self.connect() as conn:
            devices = conn.execute(
                "SELECT d.id,d.notification_preferences_json FROM devices d JOIN users u ON u.id=d.user_id "
                "WHERE u.profile_id=? AND d.revoked_at IS NULL AND d.push_token_encrypted IS NOT NULL",
                (profile,),
            ).fetchall()
            count = 0
            pref_key = {
                "run.completed": "turn_completed",
                "run.failed": "turn_failed",
                "approval.requested": "approval_required",
            }.get(kind)
            for device in devices:
                prefs = json.loads(device["notification_preferences_json"] or "{}")
                if pref_key and prefs.get(pref_key, True) is False:
                    continue
                count += conn.execute(
                    "INSERT OR IGNORE INTO notification_outbox(id,profile_id,device_id,kind,dedupe_key,"
                    "payload_json,status,next_attempt_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("out"),
                        profile,
                        device["id"],
                        kind,
                        dedupe_key,
                        json_dump(payload),
                        "pending",
                        now,
                        now,
                    ),
                ).rowcount
            return count

    def pending_push(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT o.*,d.push_token_encrypted FROM notification_outbox o JOIN devices d ON d.id=o.device_id "
                "WHERE o.status='pending' AND o.next_attempt_at<=? AND d.revoked_at IS NULL "
                "ORDER BY o.created_at LIMIT ?",
                (iso(), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def pending_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT o.*,d.push_token_encrypted FROM notification_outbox o JOIN devices d ON d.id=o.device_id "
                "WHERE o.status='receipt_pending' AND o.next_attempt_at<=? ORDER BY o.created_at LIMIT ?",
                (iso(), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def finish_push(
        self,
        outbox_id: str,
        *,
        ticket: str | None = None,
        error: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            if error and retry_at:
                conn.execute(
                    "UPDATE notification_outbox SET attempts=attempts+1,last_error_code=?,next_attempt_at=? WHERE id=?",
                    (error, iso(retry_at), outbox_id),
                )
            else:
                status = (
                    "failed" if error else "receipt_pending" if ticket else "delivered"
                )
                conn.execute(
                    "UPDATE notification_outbox SET attempts=attempts+1,status=?,provider_ticket_id=?,"
                    "last_error_code=?,next_attempt_at=?,delivered_at=? WHERE id=?",
                    (
                        status,
                        ticket,
                        error,
                        iso(utcnow() + timedelta(seconds=1)) if ticket else iso(),
                        iso() if status == "delivered" else None,
                        outbox_id,
                    ),
                )

    def finish_receipt(
        self,
        outbox_id: str,
        *,
        error: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            if retry_at:
                conn.execute(
                    "UPDATE notification_outbox SET last_error_code=?,next_attempt_at=? WHERE id=?",
                    (error, iso(retry_at), outbox_id),
                )
            else:
                conn.execute(
                    "UPDATE notification_outbox SET status=?,last_error_code=?,delivered_at=? WHERE id=?",
                    (
                        "failed" if error else "delivered",
                        error,
                        None if error else iso(),
                        outbox_id,
                    ),
                )


class ProfileStore(SQLiteStore):
    def __init__(self, profile_home: Path):
        self.profile_home = Path(profile_home)
        root = self.profile_home / "plugin-data" / "hermes-mobile"
        self.files_root = root / "files"
        super().__init__(root / "profile.db", PROFILE_SCHEMA, PROFILE_SCHEMA_VERSION)

    def initialize(self) -> None:
        super().initialize()
        for child in ("originals", "extracted", "temp"):
            path = self.files_root / child
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.chmod(0o700)
            except OSError:
                pass

    def ensure_conversation(
        self, hermes_id: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = iso()
        metadata = metadata or {}

        def _ensure(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM conversation_map WHERE hermes_session_id=?", (hermes_id,)
            ).fetchone()
            if row:
                return dict(row)
            public_id = new_id("conv")
            conn.execute(
                "INSERT INTO conversation_map(public_id,hermes_session_id,title_override,pinned,archived,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    public_id,
                    hermes_id,
                    metadata.get("title"),
                    bool(metadata.get("pinned")),
                    bool(metadata.get("archived")),
                    now,
                    now,
                ),
            )
            self._journal_conn(
                conn, "conversation", public_id, "created", {"id": public_id}
            )
            return dict(
                conn.execute(
                    "SELECT * FROM conversation_map WHERE public_id=?", (public_id,)
                ).fetchone()
            )

        return self.transaction(_ensure)

    def conversation(
        self, public_id: str, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            sql = "SELECT * FROM conversation_map WHERE public_id=?"
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            row = conn.execute(sql, (public_id,)).fetchone()
            return dict(row) if row else None

    def update_conversation(self, public_id: str, fields: dict[str, Any]) -> None:
        allowed = {
            "title_override",
            "pinned",
            "archived",
            "last_read_message_id",
            "deleted_at",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        clean["updated_at"] = iso()

        def _update(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE conversation_map SET "
                + ",".join(f"{k}=?" for k in clean)
                + " WHERE public_id=?",
                (*clean.values(), public_id),
            )
            operation = "deleted" if clean.get("deleted_at") else "updated"
            self._journal_conn(
                conn, "conversation", public_id, operation, {"id": public_id, **clean}
            )

        self.transaction(_update)

    def ensure_message(
        self,
        conversation_id: str,
        hermes_id: str,
        role: str,
        created_at: str,
        run_id: str | None = None,
    ) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT public_id FROM message_map WHERE conversation_id=? AND hermes_message_id=?",
                (conversation_id, hermes_id),
            ).fetchone()
            if row:
                return str(row["public_id"])
            public_id = new_id("msg")
            conn.execute(
                "INSERT INTO message_map(public_id,conversation_id,hermes_message_id,run_id,role,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (public_id, conversation_id, hermes_id, run_id, role, created_at),
            )
            self._journal_conn(
                conn,
                "message",
                public_id,
                "created",
                {"id": public_id, "conversation_id": conversation_id},
            )
            return public_id

    def create_run(
        self, conversation_id: str, hermes_run_id: str, client_message_id: str | None
    ) -> dict[str, Any]:
        row = {
            "public_id": new_id("run"),
            "conversation_id": conversation_id,
            "hermes_run_id": hermes_run_id,
            "client_message_id": client_message_id,
            "status": "queued",
            "created_at": iso(),
        }

        def _create(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                "INSERT INTO runs(public_id,conversation_id,hermes_run_id,client_message_id,status,created_at) "
                "VALUES (:public_id,:conversation_id,:hermes_run_id,:client_message_id,:status,:created_at)",
                row,
            )
            self._journal_conn(
                conn, "run", row["public_id"], "updated", self.run_resource(row)
            )
            return row

        return self.transaction(_create)

    def run_by_hermes_id(self, hermes_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE hermes_run_id=?", (hermes_run_id,)
            ).fetchone()
            return dict(row) if row else None

    def latest_run_for_conversation(
        self, conversation_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            return dict(row) if row else None

    def run(self, public_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE public_id=?", (public_id,)
            ).fetchone()
            return dict(row) if row else None

    def nonterminal_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM runs WHERE status NOT IN ('completed','failed','cancelled')"
                )
            ]

    def update_run(
        self,
        public_id: str,
        status: str,
        *,
        error_code: str | None = None,
        final_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        now = iso()

        def _update(conn: sqlite3.Connection) -> dict[str, Any] | None:
            started = now if status == "running" else None
            completed = now if status in {"completed", "failed", "cancelled"} else None
            conn.execute(
                "UPDATE runs SET status=?,error_code=COALESCE(?,error_code),"
                "final_message_id=COALESCE(?,final_message_id),started_at=COALESCE(started_at,?),"
                "completed_at=COALESCE(completed_at,?) WHERE public_id=?",
                (status, error_code, final_message_id, started, completed, public_id),
            )
            row = conn.execute(
                "SELECT * FROM runs WHERE public_id=?", (public_id,)
            ).fetchone()
            if row:
                self._journal_conn(
                    conn, "run", public_id, "updated", self.run_resource(dict(row))
                )
                return dict(row)
            return None

        return self.transaction(_update)

    @staticmethod
    def run_resource(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row.get("public_id"),
            "conversation_id": row.get("conversation_id"),
            "status": row.get("status"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "final_message_id": row.get("final_message_id"),
            "error": (
                {"code": row.get("error_code")} if row.get("error_code") else None
            ),
        }

    def append_event(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        now = iso()

        def _append(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT last_sequence,conversation_id FROM runs WHERE public_id=?",
                (run_id,),
            ).fetchone()
            if not row:
                raise StoreError("run_not_found")
            sequence = int(row["last_sequence"]) + 1
            event = {
                "event_id": new_id("evt"),
                "sequence": sequence,
                "type": event_type,
                "run_id": run_id,
                "conversation_id": row["conversation_id"],
                "created_at": now,
                "data": data,
            }
            conn.execute(
                "INSERT INTO run_events(event_id,run_id,sequence,type,data_json,created_at) VALUES (?,?,?,?,?,?)",
                (event["event_id"], run_id, sequence, event_type, json_dump(data), now),
            )
            conn.execute(
                "UPDATE runs SET last_sequence=? WHERE public_id=?", (sequence, run_id)
            )
            return event

        return self.transaction(_append)

    def events_after(
        self, run_id: str, event_id: str | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        with self.connect() as conn:
            sequence = 0
            reset = False
            if event_id:
                row = conn.execute(
                    "SELECT sequence FROM run_events WHERE run_id=? AND event_id=?",
                    (run_id, event_id),
                ).fetchone()
                if not row:
                    reset = True
                else:
                    sequence = int(row["sequence"])
            rows = conn.execute(
                "SELECT * FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 500",
                (run_id, sequence),
            ).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "sequence": row["sequence"],
                    "type": row["type"],
                    "run_id": run_id,
                    "created_at": row["created_at"],
                    "data": json.loads(row["data_json"]),
                }
                for row in rows
            ], reset

    def approval(self, public_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE public_id=?", (public_id,)
            ).fetchone()
            return dict(row) if row else None

    def ensure_approval(self, run_id: str, hermes_request_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT public_id FROM approvals WHERE run_id=? AND hermes_request_id=?",
                (run_id, hermes_request_id),
            ).fetchone()
            if row:
                return str(row["public_id"])
            public_id = new_id("apr")
            conn.execute(
                "INSERT INTO approvals(public_id,run_id,hermes_request_id,status,created_at) VALUES (?,?,?,?,?)",
                (public_id, run_id, hermes_request_id, "pending", iso()),
            )
            return public_id

    def resolve_approval(self, public_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE approvals SET status='resolved',resolved_at=? WHERE public_id=? AND status='pending'",
                (iso(), public_id),
            )

    def create_attachment(self, values: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        row = {
            **values,
            "public_id": values.get("public_id") or new_id("att"),
            "status": "processing",
            "created_at": now,
            "updated_at": now,
        }

        def _create(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                "INSERT INTO attachments(public_id,conversation_id,client_attachment_id,filename,safe_filename,"
                "mime_type,size,sha256,storage_path,status,created_at,updated_at) "
                "VALUES (:public_id,:conversation_id,:client_attachment_id,:filename,:safe_filename,:mime_type,"
                ":size,:sha256,:storage_path,:status,:created_at,:updated_at)",
                row,
            )
            self._journal_conn(
                conn,
                "attachment",
                row["public_id"],
                "updated",
                self.attachment_resource(row),
            )
            return row

        return self.transaction(_create)

    def attachment_by_client_id(self, client_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE client_attachment_id=?", (client_id,)
            ).fetchone()
            return dict(row) if row else None

    def attachment(
        self, public_id: str, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            sql = "SELECT * FROM attachments WHERE public_id=?"
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            row = conn.execute(sql, (public_id,)).fetchone()
            return dict(row) if row else None

    def list_attachments(
        self, conversation_id: str | None, status: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_attachment(
        self,
        public_id: str,
        status: str,
        *,
        extracted_path: str | None = None,
        error_code: str | None = None,
        deleted: bool = False,
    ) -> None:
        now = iso()

        def _update(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE attachments SET status=?,extracted_path=COALESCE(?,extracted_path),error_code=?,"
                "updated_at=?,deleted_at=? WHERE public_id=?",
                (
                    status,
                    extracted_path,
                    error_code,
                    now,
                    now if deleted else None,
                    public_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM attachments WHERE public_id=?", (public_id,)
            ).fetchone()
            op = "deleted" if deleted else "updated"
            if row:
                self._journal_conn(
                    conn,
                    "attachment",
                    public_id,
                    op,
                    self.attachment_resource(dict(row)),
                )

        self.transaction(_update)

    @staticmethod
    def attachment_resource(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row.get("public_id"),
            "conversation_id": row.get("conversation_id"),
            "filename": row.get("filename"),
            "mime_type": row.get("mime_type"),
            "size": row.get("size"),
            "sha256": row.get("sha256"),
            "status": row.get("status"),
            "error": (
                {"code": row.get("error_code")} if row.get("error_code") else None
            ),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def idempotency(
        self,
        scope: str,
        request_hash: str,
        create: Callable[[], tuple[int, dict[str, Any], str | None]],
    ) -> tuple[int, dict[str, Any], bool]:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE scope_hash=?", (scope,)
            ).fetchone()
            if row and parse_time(row["expires_at"]) > now:
                if row["request_hash"] != request_hash:
                    raise StoreError("idempotency_conflict")
                return (
                    int(row["response_status"]),
                    json.loads(row["response_json"]),
                    True,
                )
        status, response, resource_id = create()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_keys(scope_hash,request_hash,response_status,response_json,"
                "resource_id,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                (
                    scope,
                    request_hash,
                    status,
                    json_dump(response),
                    resource_id,
                    iso(now),
                    iso(now + timedelta(hours=24)),
                ),
            )
        return status, response, False

    def lookup_idempotency(
        self, scope: str, request_hash: str
    ) -> tuple[int, dict[str, Any]] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE scope_hash=?", (scope,)
            ).fetchone()
            if not row or parse_time(row["expires_at"]) <= utcnow():
                return None
            if row["request_hash"] != request_hash:
                raise StoreError("idempotency_conflict")
            return int(row["response_status"]), json.loads(row["response_json"])

    def save_idempotency(
        self,
        scope: str,
        request_hash: str,
        status: int,
        response: dict[str, Any],
        resource_id: str | None,
    ) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_keys(scope_hash,request_hash,response_status,response_json,"
                "resource_id,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                (
                    scope,
                    request_hash,
                    status,
                    json_dump(response),
                    resource_id,
                    iso(now),
                    iso(now + timedelta(hours=24)),
                ),
            )

    def sync(self, sequence: int, limit: int) -> tuple[list[dict[str, Any]], int, bool]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_journal WHERE sequence>? ORDER BY sequence LIMIT ?",
                (sequence, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            changes = [
                {
                    "type": f"{row['entity_type']}.{row['operation']}",
                    "entity": json.loads(row["payload_json"]),
                    "id": row["entity_id"],
                }
                for row in rows
            ]
            next_sequence = int(rows[-1]["sequence"]) if rows else sequence
            return changes, next_sequence, has_more

    @staticmethod
    def _journal_conn(
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        operation: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO sync_journal(entity_type,entity_id,operation,payload_json,created_at) VALUES (?,?,?,?,?)",
            (entity_type, entity_id, operation, json_dump(payload), iso()),
        )

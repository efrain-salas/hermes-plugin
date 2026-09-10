CONTROL_SCHEMA_VERSION = 2
PROFILE_SCHEMA_VERSION = 3

CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    installation_id TEXT NOT NULL,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_version TEXT,
    locale TEXT,
    timezone TEXT,
    push_provider TEXT,
    push_token_encrypted TEXT,
    notification_preferences_json TEXT NOT NULL DEFAULT '{}',
    scopes_json TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, installation_id)
);
CREATE TABLE IF NOT EXISTS pairing_tokens (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    family_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    rotated_at TEXT,
    replaced_by_id TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_device ON refresh_tokens(device_id, family_id);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    provider_ticket_id TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(device_id, kind, dedupe_key)
);
CREATE TABLE IF NOT EXISTS admin_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_bootstrap_tokens (
    token_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_webauthn_credentials (
    credential_id BLOB PRIMARY KEY,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS admin_webauthn_challenges (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    challenge BLOB NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    remote_hash TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_challenges_expiry
    ON admin_webauthn_challenges(expires_at);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at);
"""

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_map (
    public_id TEXT PRIMARY KEY,
    hermes_session_id TEXT NOT NULL UNIQUE,
    title_override TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    reasoning_effort TEXT CHECK(
        reasoning_effort IS NULL OR reasoning_effort IN (
            'none','minimal','low','medium','high','xhigh','max','ultra'
        )
    ),
    last_read_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS message_map (
    public_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversation_map(public_id),
    hermes_message_id TEXT NOT NULL,
    run_id TEXT,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, hermes_message_id)
);
CREATE TABLE IF NOT EXISTS runs (
    public_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversation_map(public_id),
    hermes_run_id TEXT NOT NULL UNIQUE,
    client_message_id TEXT,
    status TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    final_message_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(public_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
CREATE TABLE IF NOT EXISTS approvals (
    public_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(public_id) ON DELETE CASCADE,
    hermes_request_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(run_id, hermes_request_id)
);
CREATE TABLE IF NOT EXISTS attachments (
    public_id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversation_map(public_id),
    client_attachment_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    safe_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    extracted_path TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(client_attachment_id)
);
CREATE TABLE IF NOT EXISTS sync_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope_hash TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    resource_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_task_map (
    public_id TEXT PRIMARY KEY,
    hermes_job_id TEXT NOT NULL UNIQUE,
    origin_conversation_id TEXT REFERENCES conversation_map(public_id),
    conversation_policy TEXT CHECK(
        conversation_policy IS NULL OR
        conversation_policy IN ('hub_only','origin')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduled_run_state (
    public_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES scheduled_task_map(public_id) ON DELETE CASCADE,
    hermes_execution_id TEXT NOT NULL UNIQUE,
    read_at TEXT,
    conversation_delivered_at TEXT,
    notification_enqueued_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON run_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_sync_sequence ON sync_journal(sequence);
CREATE INDEX IF NOT EXISTS idx_attachments_conversation ON attachments(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_task
    ON scheduled_run_state(task_id, created_at DESC);
"""

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    event_time TEXT,
    received_at TEXT,
    persisted_at TEXT,
    source_time TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    time_source TEXT NOT NULL DEFAULT 'ingest',
    event_sequence INTEGER,
    ingest_delay_seconds REAL NOT NULL DEFAULT 0.0,
    actor_id TEXT,
    actor_role TEXT NOT NULL DEFAULT 'unknown',
    source_channel TEXT NOT NULL DEFAULT 'api',
    content_origin TEXT NOT NULL DEFAULT 'original',
    extractor TEXT,
    derived_from_json TEXT NOT NULL DEFAULT '[]',
    latest_audit_event_id TEXT,
    latest_object_digest TEXT,
    created_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_topics_json TEXT NOT NULL DEFAULT '[]',
    episodes_json TEXT NOT NULL DEFAULT '[]',
    carry_over_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodic_memories (
    id TEXT PRIMARY KEY,
    date TEXT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    entities_json TEXT NOT NULL DEFAULT '[]',
    emotion_valence TEXT NOT NULL DEFAULT 'neutral',
    emotion_intensity REAL NOT NULL DEFAULT 0.0,
    emotion_tags_json TEXT NOT NULL DEFAULT '[]',
    importance_score REAL NOT NULL DEFAULT 0.0,
    recency_score REAL NOT NULL DEFAULT 1.0,
    repetition_score REAL NOT NULL DEFAULT 0.0,
    continuity_score REAL NOT NULL DEFAULT 0.0,
    last_recalled_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    source_json TEXT NOT NULL DEFAULT '{}',
    retention_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_type TEXT NOT NULL DEFAULT 'inferred',
    wording_policy TEXT NOT NULL DEFAULT 'tentative',
    user_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL,
    current_state_json TEXT NOT NULL DEFAULT '[]',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    related_episodes_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    importance_score REAL NOT NULL DEFAULT 0.0,
    last_recalled_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    source_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_type TEXT NOT NULL DEFAULT 'inferred',
    wording_policy TEXT NOT NULL DEFAULT 'tentative',
    user_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persistent_memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'preference',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    importance_score REAL NOT NULL DEFAULT 0.85,
    last_recalled_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    source_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence_type TEXT NOT NULL DEFAULT 'explicit',
    wording_policy TEXT NOT NULL DEFAULT 'confirmed',
    user_confirmed INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_links (
    id TEXT PRIMARY KEY,
    from_memory_id TEXT NOT NULL,
    from_memory_type TEXT NOT NULL,
    to_memory_id TEXT NOT NULL,
    to_memory_type TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_history (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    query TEXT NOT NULL,
    reason TEXT NOT NULL,
    relevance REAL NOT NULL,
    inject_mode TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_traces (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    turn_id TEXT,
    trace_stage TEXT NOT NULL DEFAULT 'proto'
        CHECK (trace_stage IN ('proto', 'candidate')),
    candidate_memory_type TEXT
        CHECK (candidate_memory_type IS NULL OR candidate_memory_type IN ('episodic', 'semantic', 'prospective', 'procedural', 'embodied')),
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    acquisition_mode TEXT NOT NULL DEFAULT 'automatic'
        CHECK (acquisition_mode IN ('automatic', 'user_explicit', 'reviewed', 'system_derived')),
    epistemic_status TEXT NOT NULL DEFAULT 'inferred'
        CHECK (epistemic_status IN ('observed', 'inferred', 'confirmed', 'disputed')),
    epistemic_confidence REAL NOT NULL DEFAULT 0.5,
    activation REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.1,
    continuity_score REAL NOT NULL DEFAULT 0.0,
    retention_score REAL NOT NULL DEFAULT 0.0,
    affect_signal_json TEXT NOT NULL DEFAULT '{}',
    capture_score REAL NOT NULL DEFAULT 0.0,
    repetition_score REAL NOT NULL DEFAULT 0.0,
    unfinished_score REAL NOT NULL DEFAULT 0.0,
    confirmation_score REAL NOT NULL DEFAULT 0.0,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    extraction_reasons_json TEXT NOT NULL DEFAULT '[]',
    content_fingerprint TEXT NOT NULL DEFAULT '',
    first_observed_at TEXT,
    last_observed_at TEXT,
    event_time TEXT,
    received_at TEXT,
    persisted_at TEXT,
    source_time TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    time_source TEXT NOT NULL DEFAULT 'ingest',
    ingest_delay_seconds REAL NOT NULL DEFAULT 0.0,
    valid_from TEXT,
    valid_until TEXT,
    superseded_by TEXT,
    actor_id TEXT,
    actor_role TEXT NOT NULL DEFAULT 'system',
    source_channel TEXT NOT NULL DEFAULT 'internal',
    content_origin TEXT NOT NULL DEFAULT 'derived',
    extractor TEXT,
    derived_from_json TEXT NOT NULL DEFAULT '[]',
    latest_audit_event_id TEXT,
    latest_object_digest TEXT,
    evidence_summary TEXT NOT NULL DEFAULT '',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_json TEXT NOT NULL DEFAULT '{}',
    observation_statement TEXT,
    perspective TEXT,
    evidence_kind TEXT,
    observation_fidelity REAL,
    source_reliability REAL,
    world_hypothesis TEXT,
    record_threshold REAL NOT NULL DEFAULT 0.65,
    review_threshold REAL NOT NULL DEFAULT 0.82,
    delete_threshold REAL NOT NULL DEFAULT 0.15,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'review', 'consolidated', 'archived')),
    last_recalled_at TEXT,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_decayed_at TEXT NOT NULL,
    expires_at TEXT,
    consolidated_at TEXT,
    consolidated_memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL
        CHECK (memory_type IN ('episodic', 'semantic', 'prospective', 'procedural', 'embodied')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    entities_json TEXT NOT NULL DEFAULT '[]',
    acquisition_mode TEXT NOT NULL DEFAULT 'system_derived'
        CHECK (acquisition_mode IN ('automatic', 'user_explicit', 'reviewed', 'system_derived')),
    epistemic_status TEXT NOT NULL DEFAULT 'inferred'
        CHECK (epistemic_status IN ('observed', 'inferred', 'confirmed', 'disputed')),
    epistemic_confidence REAL NOT NULL DEFAULT 0.5,
    activation REAL NOT NULL DEFAULT 0.5,
    salience REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.3,
    continuity_score REAL NOT NULL DEFAULT 0.0,
    retention_score REAL NOT NULL DEFAULT 0.0,
    last_recalled_at TEXT,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_decayed_at TEXT NOT NULL,
    expires_at TEXT,
    event_time TEXT,
    received_at TEXT,
    persisted_at TEXT,
    source_time TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    time_source TEXT NOT NULL DEFAULT 'derived',
    ingest_delay_seconds REAL NOT NULL DEFAULT 0.0,
    valid_from TEXT,
    valid_until TEXT,
    superseded_by TEXT,
    actor_id TEXT,
    actor_role TEXT NOT NULL DEFAULT 'system',
    source_channel TEXT NOT NULL DEFAULT 'internal',
    content_origin TEXT NOT NULL DEFAULT 'derived',
    extractor TEXT,
    derived_from_json TEXT NOT NULL DEFAULT '[]',
    latest_audit_event_id TEXT,
    latest_object_digest TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    evidence_summary TEXT NOT NULL DEFAULT '',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    observation_statement TEXT,
    perspective TEXT,
    evidence_kind TEXT,
    observation_fidelity REAL,
    source_reliability REAL,
    world_hypothesis TEXT,
    consolidated_at TEXT NOT NULL,
    legacy_memory_type TEXT,
    legacy_memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (legacy_memory_type, legacy_memory_id)
);

CREATE TABLE IF NOT EXISTS memory_evidence_links (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    trace_id TEXT,
    source_event_id TEXT,
    relation TEXT NOT NULL DEFAULT 'derived_from',
    created_at TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (trace_id) REFERENCES memory_traces(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    actor_id TEXT,
    actor_role TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    content_origin TEXT NOT NULL,
    extractor TEXT,
    derivation_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    object_digest TEXT,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    event_time TEXT NOT NULL,
    received_at TEXT NOT NULL,
    persisted_at TEXT NOT NULL,
    integrity_tier TEXT NOT NULL DEFAULT 'routine'
        CHECK (integrity_tier IN ('routine', 'durable', 'privileged')),
    format_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provenance_edges (
    edge_id TEXT PRIMARY KEY,
    source_object_type TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    target_object_type TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    audit_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        source_object_type, source_object_id,
        target_object_type, target_object_id,
        relation, audit_event_id
    ),
    FOREIGN KEY (audit_event_id) REFERENCES audit_events(event_id)
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS provenance_edges_no_update
BEFORE UPDATE ON provenance_edges
BEGIN
    SELECT RAISE(ABORT, 'provenance_edges is append-only');
END;

CREATE TRIGGER IF NOT EXISTS provenance_edges_no_delete
BEFORE DELETE ON provenance_edges
BEGIN
    SELECT RAISE(ABORT, 'provenance_edges is append-only');
END;

CREATE TABLE IF NOT EXISTS signing_keys (
    key_id TEXT PRIMARY KEY,
    algorithm TEXT NOT NULL,
    public_key_b64 TEXT NOT NULL,
    predecessor_key_id TEXT,
    trust_origin TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS key_rotations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    rotation_id TEXT NOT NULL UNIQUE,
    old_key_id TEXT NOT NULL,
    new_key_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    old_signature_b64 TEXT NOT NULL,
    new_signature_b64 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_branches (
    branch_id TEXT PRIMARY KEY,
    parent_branch_id TEXT,
    fork_checkpoint_id TEXT,
    fork_checkpoint_hash TEXT,
    previous_canonical_checkpoint_id TEXT,
    previous_canonical_checkpoint_hash TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_branch_adoptions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    adoption_id TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL,
    previous_branch_id TEXT,
    reason TEXT NOT NULL,
    adopted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_checkpoints (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL,
    sequence_end INTEGER NOT NULL,
    head_event_id TEXT,
    head_event_hash TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    previous_checkpoint_id TEXT,
    previous_checkpoint_hash TEXT,
    key_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    signature_b64 TEXT NOT NULL,
    checkpoint_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS signing_keys_no_update
BEFORE UPDATE ON signing_keys
BEGIN
    SELECT RAISE(ABORT, 'signing_keys is append-only');
END;

CREATE TRIGGER IF NOT EXISTS signing_keys_no_delete
BEFORE DELETE ON signing_keys
BEGIN
    SELECT RAISE(ABORT, 'signing_keys is append-only');
END;

CREATE TRIGGER IF NOT EXISTS key_rotations_no_update
BEFORE UPDATE ON key_rotations
BEGIN
    SELECT RAISE(ABORT, 'key_rotations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS key_rotations_no_delete
BEFORE DELETE ON key_rotations
BEGIN
    SELECT RAISE(ABORT, 'key_rotations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_branches_no_update
BEFORE UPDATE ON audit_branches
BEGIN
    SELECT RAISE(ABORT, 'audit_branches is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_branches_no_delete
BEFORE DELETE ON audit_branches
BEGIN
    SELECT RAISE(ABORT, 'audit_branches is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_branch_adoptions_no_update
BEFORE UPDATE ON audit_branch_adoptions
BEGIN
    SELECT RAISE(ABORT, 'audit_branch_adoptions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_branch_adoptions_no_delete
BEFORE DELETE ON audit_branch_adoptions
BEGIN
    SELECT RAISE(ABORT, 'audit_branch_adoptions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_checkpoints_no_update
BEFORE UPDATE ON audit_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'audit_checkpoints is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_checkpoints_no_delete
BEFORE DELETE ON audit_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'audit_checkpoints is append-only');
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    memory_type UNINDEXED,
    title,
    summary,
    keywords,
    body,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_trace_fts USING fts5(
    trace_id UNINDEXED,
    title,
    content,
    keywords,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS long_term_memory_fts USING fts5(
    memory_id UNINDEXED,
    memory_type UNINDEXED,
    title,
    content,
    keywords,
    tokenize = 'unicode61'
);

CREATE INDEX IF NOT EXISTS idx_raw_messages_conversation
    ON raw_messages(conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_episodic_active
    ON episodic_memories(archived, pinned, importance_score);

CREATE INDEX IF NOT EXISTS idx_project_active
    ON project_memories(archived, status, importance_score);

CREATE INDEX IF NOT EXISTS idx_persistent_active
    ON persistent_memories(archived, pinned, importance_score);

CREATE INDEX IF NOT EXISTS idx_recall_memory
    ON recall_history(memory_type, memory_id, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_traces_lifecycle
    ON memory_traces(status, retention_score, expires_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_memory_traces_conversation
    ON memory_traces(conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_memories_retrieval
    ON memories(archived, memory_type, activation, retention_score);

CREATE INDEX IF NOT EXISTS idx_memories_epistemic
    ON memories(epistemic_status, acquisition_mode, archived);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_memory
    ON memory_evidence_links(memory_id, created_at);

CREATE INDEX IF NOT EXISTS idx_audit_events_object
    ON audit_events(object_type, object_id, sequence);

CREATE INDEX IF NOT EXISTS idx_audit_events_hash
    ON audit_events(previous_event_hash, event_hash);

CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_single_successor
    ON audit_events(previous_event_hash);

CREATE INDEX IF NOT EXISTS idx_provenance_target
    ON provenance_edges(target_object_type, target_object_id, created_at);

CREATE INDEX IF NOT EXISTS idx_provenance_source
    ON provenance_edges(source_object_type, source_object_id, created_at);

CREATE INDEX IF NOT EXISTS idx_checkpoints_branch
    ON audit_checkpoints(branch_id, sequence);

CREATE INDEX IF NOT EXISTS idx_checkpoints_head
    ON audit_checkpoints(head_event_hash, sequence_end);

CREATE INDEX IF NOT EXISTS idx_key_rotations_keys
    ON key_rotations(old_key_id, new_key_id, sequence);

CREATE INDEX IF NOT EXISTS idx_branch_adoptions_branch
    ON audit_branch_adoptions(branch_id, sequence);

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
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

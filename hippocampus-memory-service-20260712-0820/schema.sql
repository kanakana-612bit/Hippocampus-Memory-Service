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

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    memory_type UNINDEXED,
    title,
    summary,
    keywords,
    body,
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

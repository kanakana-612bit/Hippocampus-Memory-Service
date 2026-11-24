PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS v02_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO v02_meta VALUES ('schema_version','5');
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    conversation_id TEXT,
    data_json TEXT NOT NULL CHECK(json_valid(data_json)),
    version INTEGER NOT NULL CHECK(version>0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    latest_audit_event_id TEXT,
    latest_object_digest TEXT
);
CREATE INDEX IF NOT EXISTS records_scope ON records(principal_id,conversation_id,kind);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED, content, keywords);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(id UNINDEXED, content);
CREATE VIRTUAL TABLE IF NOT EXISTS raw_event_fts USING fts5(id UNINDEXED, content);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
    actor_id TEXT, actor_role TEXT NOT NULL, source_channel TEXT NOT NULL,
    content_origin TEXT NOT NULL, extractor TEXT, derivation_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, object_digest TEXT,
    previous_event_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE,
    event_time TEXT NOT NULL, received_at TEXT NOT NULL, persisted_at TEXT NOT NULL,
    integrity_tier TEXT NOT NULL, format_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provenance_edges (
    edge_id TEXT PRIMARY KEY, source_object_type TEXT NOT NULL, source_object_id TEXT NOT NULL,
    target_object_type TEXT NOT NULL, target_object_id TEXT NOT NULL, relation TEXT NOT NULL,
    audit_event_id TEXT NOT NULL REFERENCES audit_events(event_id), created_at TEXT NOT NULL,
    UNIQUE(source_object_type,source_object_id,target_object_type,target_object_id,relation,audit_event_id)
);
CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS provenance_edges_no_update BEFORE UPDATE ON provenance_edges
BEGIN SELECT RAISE(ABORT,'provenance_edges is append-only'); END;
CREATE TRIGGER IF NOT EXISTS provenance_edges_no_delete BEFORE DELETE ON provenance_edges
BEGIN SELECT RAISE(ABORT,'provenance_edges is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_records BEFORE UPDATE OF data_json ON records
WHEN OLD.kind IN ('event','revision','run','observation','feedback','receipt','migration','memory_version','erasure',
                 'knowledge_claim_version','knowledge_source','knowledge_evidence','knowledge_search','knowledge_review','schema_upgrade',
                 'knowledge_extraction','knowledge_migration',
                 'knowledge_search_plan','knowledge_search_attempt','knowledge_search_observation',
                 'episode_chunk','episode_summary','episode','structured_candidate','structured_review','artifact_reference','episode_job')
BEGIN SELECT RAISE(ABORT,'immutable canonical record'); END;

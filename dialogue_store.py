"""Canonical dialogue membership is separate from immutable text and audit drafts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from audit_ledger import AuditLedger, canonical_json, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogue_events (
    id TEXT PRIMARY KEY REFERENCES raw_messages(id),
    conversation_id TEXT NOT NULL,
    logical_actor_id TEXT,
    model_provenance_json TEXT NOT NULL DEFAULT '{}',
    source_message_id TEXT,
    created_at TEXT NOT NULL,
    latest_audit_event_id TEXT,
    latest_object_digest TEXT
);
CREATE TABLE IF NOT EXISTS dialogue_path_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    conversation_id TEXT NOT NULL,
    revision_key TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    latest_audit_event_id TEXT,
    latest_object_digest TEXT,
    UNIQUE(conversation_id, revision_key)
);
CREATE INDEX IF NOT EXISTS idx_dialogue_path_latest
    ON dialogue_path_events(conversation_id, sequence DESC);
CREATE VIEW IF NOT EXISTS canonical_dialogue_events AS
    SELECT r.* FROM raw_messages r
    WHERE r.id NOT LIKE 'candidate_audit:%' AND (
        EXISTS (
            SELECT 1 FROM dialogue_path_events p, json_each(p.event_ids_json) e
            WHERE p.conversation_id=r.conversation_id AND e.value=r.id
            AND p.sequence=(SELECT MAX(p2.sequence) FROM dialogue_path_events p2
                            WHERE p2.conversation_id=r.conversation_id)
        ) OR (
            NOT EXISTS (SELECT 1 FROM dialogue_path_events p WHERE p.conversation_id=r.conversation_id)
            AND r.source_channel NOT LIKE 'openwebui%'
            AND COALESCE(json_extract(r.meta_json, '$.path_state'), '')
                NOT IN ('generated_candidate', 'unconfirmed_generated_branch', 'branch', 'unknown')
        )
    );
"""


def initialize(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Only audit pointer fields may be updated after creation.
    for table, columns in (
        ('dialogue_events', ['id', 'conversation_id', 'logical_actor_id', 'model_provenance_json', 'source_message_id', 'created_at']),
        ('dialogue_path_events', ['sequence', 'id', 'conversation_id', 'revision_key', 'event_ids_json', 'created_at']),
    ):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable BEFORE UPDATE OF {','.join(columns)} ON {table} BEGIN SELECT RAISE(ABORT, 'dialogue records are append-only'); END")
        con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'dialogue records are append-only'); END")
    con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (9,?,?)", ('canonical dialogue and context compiler', utc_now()))


def register_event(con: sqlite3.Connection, event: dict[str, Any]) -> None:
    meta = event.get('meta') or {}
    logical = meta.get('logical_actor_id') or event.get('actor_id')
    # The host supplies a stable agent identity; unknown historical identity stays unknown.
    model = meta.get('model_provenance') or {}
    row = con.execute('SELECT * FROM dialogue_events WHERE id=?', (event['id'],)).fetchone()
    if row:
        if logical and row['logical_actor_id'] and logical != row['logical_actor_id']:
            raise ValueError('Logical actor cannot change for an existing dialogue event')
        if model and json.loads(row['model_provenance_json']) and model != json.loads(row['model_provenance_json']):
            raise ValueError('Model provenance cannot change for an existing dialogue event')
        return
    con.execute('INSERT INTO dialogue_events(id,conversation_id,logical_actor_id,model_provenance_json,source_message_id,created_at) VALUES (?,?,?,?,?,?)',
                (event['id'], event['conversation_id'], logical, canonical_json(model), meta.get('source_message_id'), utc_now()))
    AuditLedger().append_object_event(con, event_type='dialogue.registered', object_type='dialogue_event',
                                     object_id=event['id'], actor_id=logical, actor_role=event['role'],
                                     source_channel='dialogue_ingest', content_origin='original')


def select_path(con: sqlite3.Connection, conversation_id: str, event_ids: list[str], revision_key: str,
                expected_revision: str | None = None) -> dict[str, Any]:
    if len(event_ids) != len(set(event_ids)) or any(x.startswith('candidate_audit:') for x in event_ids):
        raise ValueError('Path requires unique dialogue event IDs, never audit candidates')
    serialized = canonical_json(event_ids)
    previous = con.execute('SELECT * FROM dialogue_path_events WHERE conversation_id=? AND revision_key=?', (conversation_id, revision_key)).fetchone()
    if previous:
        if previous['event_ids_json'] != serialized:
            raise ValueError('Path revision key reused with different content')
        return dict(previous)
    latest = con.execute('SELECT revision_key FROM dialogue_path_events WHERE conversation_id=? ORDER BY sequence DESC LIMIT 1', (conversation_id,)).fetchone()
    if expected_revision is not None and (latest['revision_key'] if latest else '') != expected_revision:
        raise ValueError('Stale path revision')
    for event_id in event_ids:
        row = con.execute('SELECT conversation_id FROM raw_messages WHERE id=?', (event_id,)).fetchone()
        if not row or row['conversation_id'] != conversation_id:
            raise ValueError('Path contains a missing or cross-conversation event')
    identity = 'path_' + uuid.uuid4().hex
    con.execute('INSERT INTO dialogue_path_events(id,conversation_id,revision_key,event_ids_json,created_at) VALUES (?,?,?,?,?)',
                (identity, conversation_id, revision_key, serialized, utc_now()))
    AuditLedger().append_object_event(con, event_type='dialogue.path_selected', object_type='dialogue_path',
                                     object_id=identity, actor_role='system', source_channel='frontend_path',
                                     content_origin='observed', payload={'event_count': len(event_ids)})
    return dict(con.execute('SELECT * FROM dialogue_path_events WHERE id=?', (identity,)).fetchone())


def source_ids(item: dict[str, Any]) -> set[str]:
    ids = set(item.get('source_event_ids') or json.loads(item.get('source_event_ids_json') or '[]'))
    source = item.get('source') or json.loads(item.get('source_json') or '{}')
    for key in ('message_id', 'source_message_id'):
        if source.get(key):
            ids.add(str(source[key]))
    for key in ('source_event_ids', 'message_ids', 'source_message_ids'):
        ids.update(source.get(key) or [])
    return ids


def sources_eligible(con: sqlite3.Connection, item: dict[str, Any], seen: set | None = None) -> bool:
    """Follow every derivation, including already-consolidated intermediate memories."""
    seen = set() if seen is None else set(seen)
    identity = (item.get('id'), item.get('memory_type', item.get('candidate_memory_type')))
    if identity in seen:
        return False
    seen.add(identity)
    ids = source_ids(item)
    derivations = list(item.get('derived_from') or json.loads(item.get('derived_from_json') or '[]'))
    if item.get('id'):
        for edge in con.execute("SELECT source_object_type,source_object_id FROM provenance_edges WHERE target_object_id=? AND relation NOT IN ('consolidated_into', 'superseded_by', 'supersedes')", (item['id'],)):
            derivations.append({'object_type': edge[0], 'object_id': edge[1]})
    for link in derivations:
        if link.get('relation') in ('consolidated_into', 'superseded_by', 'supersedes'):
            continue
        kind, event_id = link.get('object_type'), link.get('object_id')
        if str(event_id).startswith('candidate_audit:') or kind == 'generation_candidate':
            return False
        if kind == 'raw_message':
            ids.add(event_id)
        elif kind in ('memory', 'memory_trace'):
            table = 'memories' if kind == 'memory' else 'memory_traces'
            row = con.execute(f'SELECT * FROM {table} WHERE id=?', (event_id,)).fetchone()
            if row is None or not sources_eligible(con, dict(row), seen):
                return False
        else:
            return False
    source = item.get('source') or json.loads(item.get('source_json') or '{}')
    conversation = source.get('conversation_id') or item.get('conversation_id')
    if not ids and not derivations and conversation and con.execute('SELECT 1 FROM raw_messages WHERE conversation_id=? LIMIT 1', (conversation,)).fetchone():
        return False
    for event_id in ids:
        if str(event_id).startswith('candidate_audit:'):
            return False
        if not con.execute('SELECT 1 FROM canonical_dialogue_events WHERE id=?', (event_id,)).fetchone():
            return False
    return True


def reject_candidate_sources(item: dict[str, Any]) -> None:
    values = [item.get('id', ''), *source_ids(item)]
    for link in item.get('derived_from') or []:
        values.append(link.get('object_id', ''))
        if link.get('object_type') == 'generation_candidate':
            raise ValueError('Audit candidates cannot be memory sources')
    if any(str(x).startswith('candidate_audit:') for x in values):
        raise ValueError('Audit candidates cannot be memory sources')


class CandidateAuditStore:
    """Separate file, no ATTACH and no normal-retrieval connection."""
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute('CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL)')
        return con

    def save(self, conversation_id: str, content: str, metadata: dict, candidate_id: str) -> dict:
        if not candidate_id.startswith('candidate_audit:'):
            raise ValueError('Candidate IDs require candidate_audit: namespace')
        digest = hashlib.sha256(content.encode()).hexdigest()
        con = self.connect()
        try:
            row = con.execute('SELECT * FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            if row and (row['digest'] != digest or row['conversation_id'] != conversation_id or row['metadata_json'] != canonical_json(metadata)):
                raise ValueError('Candidate ID collision')
            con.execute('INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?)', (candidate_id, conversation_id, content, digest, canonical_json(metadata), utc_now()))
            con.commit()
            return {'id': candidate_id, 'digest': digest, 'scope': 'debug_only'}
        finally:
            con.close()

    def list(self, conversation_id: str, limit: int = 50) -> list[dict]:
        con = self.connect()
        try:
            return [dict(r) for r in con.execute('SELECT * FROM candidates WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?', (conversation_id, max(1, min(limit, 100))))]
        finally:
            con.close()

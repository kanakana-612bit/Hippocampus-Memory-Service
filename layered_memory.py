from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit_ledger import AuditLedger
from attribution_gate import AttributionGate
from backup_restore import BackupRestore
from checkpoint_security import CheckpointSecurity
from temporal_memory import (
    TEMPORAL_SCOPES,
    normalize_timestamp,
    optional_timestamp,
    seconds_between,
    temporal_relevance,
    temporal_state,
    timezone_name,
    validate_window,
)


MEMORY_TYPES = ("episodic", "semantic", "prospective", "procedural", "embodied")
LEGACY_MEMORY_TYPES = ("episodic", "project", "persistent")
LEGACY_TYPE_MAP = {
    "episodic": "episodic",
    "project": "prospective",
    "persistent": "semantic",
}
ACQUISITION_MODES = ("automatic", "user_explicit", "reviewed", "system_derived")
EPISTEMIC_STATUSES = ("observed", "inferred", "confirmed", "disputed")
TRACE_STAGES = ("proto", "candidate")
TRACE_STATUSES = ("active", "review", "consolidated", "archived")

DEFAULT_RECORD_THRESHOLD = 0.65
DEFAULT_REVIEW_THRESHOLD = 0.82
DEFAULT_DELETE_THRESHOLD = 0.15
DEFAULT_DAILY_DECAY_RATE = 0.90
DEFAULT_RECALL_BOOST = 0.10
DEFAULT_SPACED_STABILITY_BOOST = 0.025
PHASE_1_SCHEMA_VERSION = 2
PHASE_1_REQUIRED_OBJECTS = (
    "memory_traces",
    "memories",
    "memory_trace_fts",
    "long_term_memory_fts",
    "memory_evidence_links",
    "recall_history",
)
PHASE_2_SCHEMA_VERSION = 3
PHASE_2_TRACE_COLUMNS = {
    "capture_score": "REAL NOT NULL DEFAULT 0.0",
    "repetition_score": "REAL NOT NULL DEFAULT 0.0",
    "unfinished_score": "REAL NOT NULL DEFAULT 0.0",
    "confirmation_score": "REAL NOT NULL DEFAULT 0.0",
    "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
    "extraction_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
    "content_fingerprint": "TEXT NOT NULL DEFAULT ''",
    "first_observed_at": "TEXT",
    "last_observed_at": "TEXT",
}
PHASE_3_SCHEMA_VERSION = 4
PHASE_3_RAW_COLUMNS = {
    "event_time": "TEXT",
    "received_at": "TEXT",
    "persisted_at": "TEXT",
    "source_time": "TEXT",
    "timezone": "TEXT NOT NULL DEFAULT 'UTC'",
    "time_source": "TEXT NOT NULL DEFAULT 'legacy_backfill'",
    "event_sequence": "INTEGER",
    "ingest_delay_seconds": "REAL NOT NULL DEFAULT 0.0",
}
PHASE_3_TEMPORAL_COLUMNS = {
    "event_time": "TEXT",
    "received_at": "TEXT",
    "persisted_at": "TEXT",
    "source_time": "TEXT",
    "timezone": "TEXT NOT NULL DEFAULT 'UTC'",
    "time_source": "TEXT NOT NULL DEFAULT 'legacy_backfill'",
    "ingest_delay_seconds": "REAL NOT NULL DEFAULT 0.0",
    "valid_from": "TEXT",
    "valid_until": "TEXT",
    "superseded_by": "TEXT",
}
PHASE_4_SCHEMA_VERSION = 5
PHASE_4_PROVENANCE_COLUMNS = {
    "actor_id": "TEXT",
    "actor_role": "TEXT NOT NULL DEFAULT 'unknown'",
    "source_channel": "TEXT NOT NULL DEFAULT 'internal'",
    "content_origin": "TEXT NOT NULL DEFAULT 'derived'",
    "extractor": "TEXT",
    "derived_from_json": "TEXT NOT NULL DEFAULT '[]'",
    "latest_audit_event_id": "TEXT",
    "latest_object_digest": "TEXT",
}
PHASE_5_SCHEMA_VERSION = 6
PHASE_5_TABLES = {
    "signing_keys",
    "key_rotations",
    "audit_branches",
    "audit_branch_adoptions",
    "audit_checkpoints",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = lo
    return max(lo, min(hi, numeric))


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def provenance_values(
    item: dict[str, Any],
    *,
    default_actor_role: str = "system",
    default_source_channel: str = "internal",
    default_content_origin: str = "derived",
) -> dict[str, Any]:
    source = dict(item.get("source") or {})
    derived_from = item.get("derived_from")
    if derived_from is None:
        derived_from = [
            {
                "object_type": "raw_message",
                "object_id": str(event_id),
                "relation": "derived_from",
            }
            for event_id in item.get("source_event_ids") or []
        ]
    return {
        "actor_id": item.get("actor_id") or source.get("actor_id") or source.get("user_id"),
        "actor_role": str(
            item.get("actor_role") or source.get("actor_role") or default_actor_role
        ),
        "source_channel": str(
            item.get("source_channel")
            or source.get("source_channel")
            or source.get("source")
            or default_source_channel
        ),
        "content_origin": str(
            item.get("content_origin")
            or source.get("content_origin")
            or default_content_origin
        ),
        "extractor": item.get("extractor") or source.get("extractor"),
        "derived_from": derived_from or [],
    }
def parse_time(value: str | None, fallback: datetime | None = None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback or datetime.now(timezone.utc)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def query_terms(text: str) -> list[str]:
    normalized = normalize_text(text)
    ascii_terms = re.findall(r"[a-z0-9_./:-]{2,}", normalized)
    quoted_terms = re.findall(r"[「『\"]([^」』\"]{2,})[」』\"]", text or "")
    japanese_chunks = re.findall(r"[\u3040-\u30ff\u3400-\u9fffー・]{2,}", text or "")
    japanese_terms: list[str] = []
    for chunk in japanese_chunks:
        japanese_terms.append(chunk)
        parts = re.split(r"(?:について|として|から|まで|より|など|の|と|に|を|は|が|で|へ|や|も|、|。)", chunk)
        japanese_terms.extend(part for part in parts if len(part) >= 2)
    seen: set[str] = set()
    result: list[str] = []
    for term in ascii_terms + quoted_terms + japanese_terms:
        normalized_term = normalize_text(term)
        if normalized_term and normalized_term not in seen:
            seen.add(normalized_term)
            result.append(normalized_term)
    return result[:20]


def retention_score(
    activation: Any,
    salience: Any,
    stability: Any,
    continuity_score: Any = 0.0,
) -> float:
    return clamp(
        0.35 * clamp(activation)
        + 0.30 * clamp(salience)
        + 0.25 * clamp(stability)
        + 0.10 * clamp(continuity_score)
    )


def decay_activation(
    activation: Any,
    elapsed_days: float,
    salience: Any,
    stability: Any,
    daily_decay_rate: float = DEFAULT_DAILY_DECAY_RATE,
) -> float:
    if elapsed_days <= 0:
        return clamp(activation)
    effective_rate = clamp(
        daily_decay_rate + 0.035 * clamp(salience) + 0.045 * clamp(stability),
        0.0,
        0.995,
    )
    return clamp(clamp(activation) * math.pow(effective_rate, elapsed_days))


def _as_optional_score(value: Any) -> float | None:
    return None if value is None else clamp(value)


def temporal_values(
    item: dict[str, Any],
    *,
    created_at: str,
    persisted_at: str,
    default_time_source: str,
) -> dict[str, Any]:
    zone = timezone_name(item.get("timezone") or "UTC")
    event_time = normalize_timestamp(
        item.get("event_time") or item.get("first_observed_at") or created_at,
        zone,
        field_name="event_time",
    )
    received_at = normalize_timestamp(
        item.get("received_at") or created_at,
        zone,
        field_name="received_at",
    )
    persisted = normalize_timestamp(
        item.get("persisted_at") or persisted_at,
        zone,
        field_name="persisted_at",
    )
    source_time = optional_timestamp(
        item.get("source_time"),
        zone,
        field_name="source_time",
    ) or event_time
    valid_from = optional_timestamp(item.get("valid_from"), zone, field_name="valid_from")
    valid_until = optional_timestamp(item.get("valid_until"), zone, field_name="valid_until")
    validate_window(valid_from, valid_until)
    return {
        "event_time": event_time,
        "received_at": received_at,
        "persisted_at": persisted,
        "source_time": source_time,
        "timezone": zone,
        "time_source": str(item.get("time_source") or default_time_source),
        "ingest_delay_seconds": seconds_between(received_at, event_time) or 0.0,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "superseded_by": item.get("superseded_by"),
    }


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back a context transaction, then release the DB handle."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class LayeredMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.audit = AuditLedger()
        self.security = CheckpointSecurity(self.db_path, self.audit)
        self.backups = BackupRestore(self.db_path, self.security, self.audit)
        self.attribution_gate = AttributionGate(self.security, self.audit)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self, con: sqlite3.Connection | None = None) -> dict[str, int]:
        own = con is None
        con = con or self.connect()
        try:
            self.migrate_phase2_schema(con)
            self.migrate_phase3_schema(con)
            self.migrate_phase4_schema(con)
            migrated = self.migrate_legacy_memories(con)
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
                (PHASE_1_SCHEMA_VERSION, "separate short-term traces and canonical long-term memories", utc_now()),
            )
            self.migrate_phase5_schema(con)
            if own:
                con.commit()
            return migrated
        finally:
            if own:
                con.close()

    @staticmethod
    def audit_enabled(con: sqlite3.Connection) -> bool:
        return con.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (PHASE_4_SCHEMA_VERSION,),
        ).fetchone() is not None

    def phase1_status(self) -> dict[str, Any]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE name IN ({})".format(
                    ",".join("?" for _ in PHASE_1_REQUIRED_OBJECTS)
                ),
                PHASE_1_REQUIRED_OBJECTS,
            ).fetchall()
            present = {str(row["name"]) for row in rows}
            migration = con.execute(
                "SELECT version, description, applied_at FROM schema_migrations WHERE version=?",
                (PHASE_1_SCHEMA_VERSION,),
            ).fetchone()

        missing = [name for name in PHASE_1_REQUIRED_OBJECTS if name not in present]
        return {
            "phase": 1,
            "name": "layered memory lifecycle",
            "complete": migration is not None and not missing,
            "schema_version": PHASE_1_SCHEMA_VERSION,
            "migration": dict(migration) if migration is not None else None,
            "required_objects": list(PHASE_1_REQUIRED_OBJECTS),
            "missing_objects": missing,
            "capabilities": {
                "separate_short_and_long_term_storage": "memory_traces" in present and "memories" in present,
                "decay_and_recall": "recall_history" in present,
                "consolidation_with_evidence": "memory_evidence_links" in present,
                "explicit_confirmed_memory": "memories" in present,
                "keyword_fts_retrieval": "memory_trace_fts" in present and "long_term_memory_fts" in present,
            },
        }

    def migrate_phase2_schema(self, con: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(memory_traces)").fetchall()
        }
        for column, definition in PHASE_2_TRACE_COLUMNS.items():
            if column not in existing:
                con.execute(f"ALTER TABLE memory_traces ADD COLUMN {column} {definition}")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_traces_fingerprint "
            "ON memory_traces(content_fingerprint, status, updated_at)"
        )
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
            (PHASE_2_SCHEMA_VERSION, "automatic candidate capture signals and reinforcement", utc_now()),
        )

    def phase2_status(self) -> dict[str, Any]:
        with self.connect() as con:
            existing = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(memory_traces)").fetchall()
            }
            migration = con.execute(
                "SELECT version, description, applied_at FROM schema_migrations WHERE version=?",
                (PHASE_2_SCHEMA_VERSION,),
            ).fetchone()
            fingerprint_index = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_traces_fingerprint'"
            ).fetchone()

        missing = [column for column in PHASE_2_TRACE_COLUMNS if column not in existing]
        phase1 = self.phase1_status()
        return {
            "phase": 2,
            "name": "automatic memory encoding",
            "complete": phase1["complete"] and migration is not None and not missing and fingerprint_index is not None,
            "schema_version": PHASE_2_SCHEMA_VERSION,
            "migration": dict(migration) if migration is not None else None,
            "missing_columns": missing,
            "capabilities": {
                "automatic_candidate_capture": not missing,
                "affect_scoring": "capture_score" in existing,
                "repetition_reinforcement": "repetition_score" in existing and fingerprint_index is not None,
                "unfinished_item_detection": "unfinished_score" in existing,
                "confirmation_signal": "confirmation_score" in existing,
                "source_event_deduplication": "content_fingerprint" in existing,
                "inferred_by_default": True,
            },
        }

    @staticmethod
    def _add_missing_columns(
        con: sqlite3.Connection,
        table: str,
        definitions: dict[str, str],
    ) -> set[str]:
        existing = {
            str(row["name"])
            for row in con.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in definitions.items():
            if column not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                existing.add(column)
        return existing

    def _backfill_phase3_temporal_data(self, con: sqlite3.Connection) -> None:
        con.execute(
            """
            UPDATE raw_messages
            SET event_time=COALESCE(event_time, created_at),
                received_at=COALESCE(received_at, created_at),
                persisted_at=COALESCE(persisted_at, created_at),
                source_time=COALESCE(source_time, created_at),
                timezone=COALESCE(NULLIF(timezone, ''), 'UTC'),
                time_source=COALESCE(NULLIF(time_source, ''), 'legacy_backfill'),
                ingest_delay_seconds=COALESCE(ingest_delay_seconds, 0.0)
            """
        )
        con.execute(
            """
            UPDATE memory_traces
            SET event_time=COALESCE(event_time, first_observed_at, created_at),
                received_at=COALESCE(received_at, created_at),
                persisted_at=COALESCE(persisted_at, created_at),
                source_time=COALESCE(source_time, first_observed_at, created_at),
                timezone=COALESCE(NULLIF(timezone, ''), 'UTC'),
                time_source=COALESCE(NULLIF(time_source, ''), 'legacy_backfill'),
                ingest_delay_seconds=COALESCE(ingest_delay_seconds, 0.0)
            """
        )
        con.execute(
            """
            UPDATE memories
            SET event_time=COALESCE(event_time, created_at),
                received_at=COALESCE(received_at, created_at),
                persisted_at=COALESCE(persisted_at, consolidated_at, created_at),
                source_time=COALESCE(source_time, created_at),
                timezone=COALESCE(NULLIF(timezone, ''), 'UTC'),
                time_source=COALESCE(NULLIF(time_source, ''), 'legacy_backfill'),
                ingest_delay_seconds=COALESCE(ingest_delay_seconds, 0.0)
            """
        )

        for table, fields in (
            (
                "raw_messages",
                ("event_time", "received_at", "persisted_at", "source_time"),
            ),
            (
                "memory_traces",
                (
                    "event_time",
                    "received_at",
                    "persisted_at",
                    "source_time",
                    "valid_from",
                    "valid_until",
                ),
            ),
            (
                "memories",
                (
                    "event_time",
                    "received_at",
                    "persisted_at",
                    "source_time",
                    "valid_from",
                    "valid_until",
                ),
            ),
        ):
            rows = con.execute(
                f"SELECT id, timezone, {', '.join(fields)} FROM {table}"
            ).fetchall()
            for row in rows:
                updates = {
                    field: normalize_timestamp(row[field], row["timezone"] or "UTC", field_name=field)
                    for field in fields
                    if row[field]
                }
                if updates:
                    assignments = ", ".join(f"{field}=?" for field in updates)
                    con.execute(
                        f"UPDATE {table} SET {assignments} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )

        sequence = 0
        conversation: str | None = None
        rows = con.execute(
            """
            SELECT rowid, conversation_id
            FROM raw_messages
            ORDER BY conversation_id, event_time, created_at, rowid
            """
        ).fetchall()
        for row in rows:
            current = str(row["conversation_id"])
            if current != conversation:
                conversation = current
                sequence = 1
            else:
                sequence += 1
            con.execute(
                "UPDATE raw_messages SET event_sequence=COALESCE(event_sequence, ?) WHERE rowid=?",
                (sequence, row["rowid"]),
            )

    def migrate_phase3_schema(self, con: sqlite3.Connection) -> None:
        self._add_missing_columns(con, "raw_messages", PHASE_3_RAW_COLUMNS)
        self._add_missing_columns(con, "memory_traces", PHASE_3_TEMPORAL_COLUMNS)
        self._add_missing_columns(con, "memories", PHASE_3_TEMPORAL_COLUMNS)

        migration_done = con.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (PHASE_3_SCHEMA_VERSION,),
        ).fetchone()
        if migration_done is None:
            self._backfill_phase3_temporal_data(con)

        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_messages_sequence "
            "ON raw_messages(conversation_id, event_sequence)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_traces_temporal "
            "ON memory_traces(valid_from, valid_until, superseded_by, event_time)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_temporal "
            "ON memories(valid_from, valid_until, superseded_by, event_time)"
        )
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
            (PHASE_3_SCHEMA_VERSION, "multiple timestamps, validity windows, and temporal context", utc_now()),
        )

    def phase3_status(self) -> dict[str, Any]:
        with self.connect() as con:
            columns = {
                table: {
                    str(row["name"])
                    for row in con.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for table in ("raw_messages", "memory_traces", "memories")
            }
            migration = con.execute(
                "SELECT version, description, applied_at FROM schema_migrations WHERE version=?",
                (PHASE_3_SCHEMA_VERSION,),
            ).fetchone()
            indexes = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?,?,?)",
                    (
                        "idx_raw_messages_sequence",
                        "idx_memory_traces_temporal",
                        "idx_memories_temporal",
                    ),
                ).fetchall()
            }

        missing = {
            "raw_messages": [name for name in PHASE_3_RAW_COLUMNS if name not in columns["raw_messages"]],
            "memory_traces": [name for name in PHASE_3_TEMPORAL_COLUMNS if name not in columns["memory_traces"]],
            "memories": [name for name in PHASE_3_TEMPORAL_COLUMNS if name not in columns["memories"]],
        }
        required_indexes = {
            "idx_raw_messages_sequence",
            "idx_memory_traces_temporal",
            "idx_memories_temporal",
        }
        phase2 = self.phase2_status()
        return {
            "phase": 3,
            "name": "temporal awareness",
            "complete": (
                phase2["complete"]
                and migration is not None
                and not any(missing.values())
                and required_indexes <= indexes
            ),
            "schema_version": PHASE_3_SCHEMA_VERSION,
            "migration": dict(migration) if migration is not None else None,
            "missing_columns": missing,
            "missing_indexes": sorted(required_indexes - indexes),
            "capabilities": {
                "multiple_event_timestamps": not missing["raw_messages"],
                "timezone_normalization": "timezone" in columns["raw_messages"],
                "ordered_conversation_events": "idx_raw_messages_sequence" in indexes,
                "temporal_context": "event_sequence" in columns["raw_messages"],
                "validity_windows": "valid_from" in columns["memories"] and "valid_until" in columns["memories"],
                "supersession": "superseded_by" in columns["memories"],
                "as_of_retrieval": "idx_memories_temporal" in indexes,
                "historical_import_timestamps": "source_time" in columns["raw_messages"],
                "monotonic_processing_duration": True,
            },
        }

    def migrate_phase4_schema(self, con: sqlite3.Connection) -> None:
        for table in ("raw_messages", "memory_traces", "memories"):
            self._add_missing_columns(con, table, PHASE_4_PROVENANCE_COLUMNS)

        con.executescript(
            """
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
            """
        )
        migration_done = self.audit_enabled(con)
        if migration_done:
            return

        con.execute(
            """
            UPDATE raw_messages
            SET actor_role=CASE
                    WHEN actor_role IS NULL OR actor_role='' OR actor_role='unknown'
                    THEN COALESCE(NULLIF(role, ''), 'unknown') ELSE actor_role END,
                source_channel=CASE
                    WHEN source_channel IS NULL OR source_channel='' OR source_channel='api'
                    THEN 'legacy_import' ELSE source_channel END,
                content_origin=CASE
                    WHEN content_origin IS NULL OR content_origin=''
                    THEN 'original' ELSE content_origin END,
                derived_from_json=COALESCE(NULLIF(derived_from_json, ''), '[]')
            """
        )
        for table in ("memory_traces", "memories"):
            con.execute(
                f"""
                UPDATE {table}
                SET actor_role=COALESCE(NULLIF(actor_role, ''), 'system'),
                    source_channel=COALESCE(NULLIF(source_channel, ''), 'legacy_migration'),
                    content_origin=COALESCE(NULLIF(content_origin, ''), 'derived'),
                    derived_from_json=COALESCE(NULLIF(derived_from_json, ''), '[]')
                """
            )

        for table, object_type in (
            ("raw_messages", "raw_message"),
            ("memory_traces", "memory_trace"),
            ("memories", "memory"),
        ):
            rows = con.execute(f"SELECT * FROM {table} ORDER BY created_at, id").fetchall()
            for row in rows:
                source = loads(row["meta_json"], {}) if table == "raw_messages" else loads(row["source_json"], {})
                source_event_ids = (
                    []
                    if table == "raw_messages"
                    else loads(row["source_event_ids_json"], [])
                )
                existing_derivations = loads(row["derived_from_json"], [])
                provenance = provenance_values(
                    {
                        "source": source,
                        "actor_id": row["actor_id"] or source.get("actor_id") or source.get("user_id"),
                        "actor_role": (
                            row["role"]
                            if table == "raw_messages"
                            else source.get("actor_role") or row["actor_role"] or "system"
                        ),
                        "source_channel": (
                            source.get("source_channel")
                            or source.get("source")
                            or row["source_channel"]
                            or "legacy_migration"
                        ),
                        "content_origin": (
                            source.get("content_origin")
                            or ("original" if table == "raw_messages" else row["content_origin"] or "derived")
                        ),
                        "extractor": row["extractor"] or source.get("extractor"),
                        "source_event_ids": source_event_ids,
                        "derived_from": existing_derivations or None,
                    },
                    default_actor_role=row["role"] if table == "raw_messages" else "system",
                    default_source_channel="legacy_migration",
                    default_content_origin="original" if table == "raw_messages" else "derived",
                )
                con.execute(
                    f"""
                    UPDATE {table}
                    SET actor_id=?, actor_role=?, source_channel=?, content_origin=?,
                        extractor=?, derived_from_json=?
                    WHERE id=?
                    """,
                    (
                        provenance["actor_id"],
                        provenance["actor_role"],
                        provenance["source_channel"],
                        provenance["content_origin"],
                        provenance["extractor"],
                        dumps(provenance["derived_from"]),
                        row["id"],
                    ),
                )
                self.audit.append_object_event(
                    con,
                    event_type=f"{object_type}.baseline",
                    object_type=object_type,
                    object_id=row["id"],
                    actor_id=provenance["actor_id"],
                    actor_role=provenance["actor_role"],
                    source_channel="phase4_migration",
                    content_origin=provenance["content_origin"],
                    extractor=provenance["extractor"],
                    derivations=provenance["derived_from"],
                    event_time=row["event_time"] or row["created_at"],
                    received_at=row["received_at"] or row["created_at"],
                    integrity_tier="durable" if object_type == "memory" else "routine",
                    payload={"baseline": True, "pre_phase4_history_unverified": True},
                )

        con.execute(
            "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
            (
                PHASE_4_SCHEMA_VERSION,
                "source attribution, append-only audit ledger, and hash chain",
                utc_now(),
            ),
        )

    def phase4_status(self) -> dict[str, Any]:
        required_tables = {"audit_events", "provenance_edges"}
        required_triggers = {
            "audit_events_no_update",
            "audit_events_no_delete",
            "provenance_edges_no_update",
            "provenance_edges_no_delete",
        }
        required_indexes = {
            "idx_audit_events_object",
            "idx_audit_events_hash",
            "idx_audit_events_single_successor",
            "idx_provenance_target",
            "idx_provenance_source",
        }
        with self.connect() as con:
            tables = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                    tuple(required_tables),
                ).fetchall()
            }
            triggers = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            indexes = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            columns = {
                table: {
                    str(row["name"])
                    for row in con.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for table in ("raw_messages", "memory_traces", "memories")
            }
            migration = con.execute(
                "SELECT version, description, applied_at FROM schema_migrations WHERE version=?",
                (PHASE_4_SCHEMA_VERSION,),
            ).fetchone()
            verification = self.audit.verify(con) if required_tables <= tables else None
        missing_columns = {
            table: [name for name in PHASE_4_PROVENANCE_COLUMNS if name not in names]
            for table, names in columns.items()
        }
        phase3 = self.phase3_status()
        return {
            "phase": 4,
            "name": "provenance and tamper-evident audit",
            "complete": (
                phase3["complete"]
                and migration is not None
                and required_tables <= tables
                and required_triggers <= triggers
                and required_indexes <= indexes
                and not any(missing_columns.values())
                and verification is not None
                and verification["valid"]
            ),
            "schema_version": PHASE_4_SCHEMA_VERSION,
            "migration": dict(migration) if migration else None,
            "missing_tables": sorted(required_tables - tables),
            "missing_triggers": sorted(required_triggers - triggers),
            "missing_indexes": sorted(required_indexes - indexes),
            "missing_columns": missing_columns,
            "verification": verification,
            "capabilities": {
                "source_attribution": not any(missing_columns.values()),
                "provenance_graph": "provenance_edges" in tables,
                "append_only_ledger": required_triggers <= triggers,
                "hash_chain": "audit_events" in tables and "idx_audit_events_single_successor" in indexes,
                "current_state_digest_verification": bool(verification),
                "signed_checkpoints": False,
            },
        }

    def migrate_phase5_schema(self, con: sqlite3.Connection) -> None:
        self.security.migrate_schema(con)
        self.security.ensure_identity(con)
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
            (
                PHASE_5_SCHEMA_VERSION,
                "signed checkpoints, key rotation, history branches, and verified restore",
                utc_now(),
            ),
        )

    def ensure_phase5_ready(self) -> dict[str, Any]:
        anchor = self.security.read_anchor()
        with self.connect() as con:
            checkpoint_count = int(
                con.execute("SELECT count(*) FROM audit_checkpoints").fetchone()[0]
            )
            if checkpoint_count == 0:
                if anchor is not None:
                    return self.phase5_status()
                checkpoint = self.security.create_checkpoint(
                    con, reason="phase5_initial_checkpoint"
                )
                anchor_document = self.security.anchor_document(
                    con, checkpoint["checkpoint_id"]
                )
            elif anchor is None:
                checkpoint = self.security.latest_checkpoint(con)
                if checkpoint is None:
                    return self.phase5_status()
                anchor_document = self.security.anchor_document(
                    con, checkpoint["checkpoint_id"]
                )
            else:
                return self.phase5_status()
        self.security.write_anchor(anchor_document)
        return self.phase5_status()

    def phase5_status(self) -> dict[str, Any]:
        required_triggers = {
            f"{table}_{operation}"
            for table in PHASE_5_TABLES
            for operation in ("no_update", "no_delete")
        }
        required_indexes = {
            "idx_checkpoints_branch",
            "idx_checkpoints_head",
            "idx_key_rotations_keys",
            "idx_branch_adoptions_branch",
        }
        with self.connect() as con:
            tables = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            triggers = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            indexes = {
                str(row["name"])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            migration = con.execute(
                "SELECT version, description, applied_at FROM schema_migrations WHERE version=?",
                (PHASE_5_SCHEMA_VERSION,),
            ).fetchone()
            identity = (
                self.security.identity_status(con)
                if PHASE_5_TABLES <= tables
                else None
            )
            verification = (
                self.security.verify_checkpoints(con)
                if PHASE_5_TABLES <= tables
                else None
            )
        phase4 = self.phase4_status()
        complete = bool(
            phase4["complete"]
            and migration is not None
            and PHASE_5_TABLES <= tables
            and required_triggers <= triggers
            and required_indexes <= indexes
            and identity
            and identity["signing_available"]
            and verification
            and verification["valid"]
        )
        return {
            "phase": 5,
            "name": "signed checkpoints and verified restore",
            "complete": complete,
            "schema_version": PHASE_5_SCHEMA_VERSION,
            "migration": dict(migration) if migration else None,
            "missing_tables": sorted(PHASE_5_TABLES - tables),
            "missing_triggers": sorted(required_triggers - triggers),
            "missing_indexes": sorted(required_indexes - indexes),
            "identity": identity,
            "verification": verification,
            "paths": {
                "security_directory": str(self.security.security_dir),
                "anchor_path": str(self.security.anchor_path),
                "backup_directory": str(self.backups.backup_dir),
            },
            "capabilities": {
                "ed25519_signed_checkpoints": bool(
                    verification and verification["checkpoint_chain_valid"]
                ),
                "external_anchor": bool(verification and verification["anchor_valid"]),
                "rollback_detection": bool(verification),
                "dual_signed_key_rotation": "key_rotations" in tables,
                "explicit_history_branches": "audit_branches" in tables,
                "signed_backup_manifest": True,
                "offline_verified_restore": True,
            },
        }

    def create_signed_checkpoint(self, reason: str = "manual") -> dict[str, Any]:
        existing_anchor = self.security.read_anchor()
        with self.connect() as con:
            checkpoint_count = int(
                con.execute("SELECT count(*) FROM audit_checkpoints").fetchone()[0]
            )
            if existing_anchor is not None:
                anchor_verification = self.security.verify_anchor_document(existing_anchor)
                if not anchor_verification["valid"]:
                    raise RuntimeError("External anchor is invalid; refusing to replace it")
                if checkpoint_count:
                    current = self.security.verify_checkpoints(con, anchor=existing_anchor)
                    if current["rollback_detected"]:
                        raise RuntimeError(
                            "Database rollback detected; adopt a restore branch before signing"
                        )
            checkpoint = self.security.create_checkpoint(con, reason=reason)
            anchor = self.security.anchor_document(con, checkpoint["checkpoint_id"])
        self.security.write_anchor(anchor)
        return {"checkpoint": checkpoint, "anchor": self.security.verify_anchor_document(anchor)}

    def verify_checkpoints(self) -> dict[str, Any]:
        with self.connect() as con:
            return self.security.verify_checkpoints(con)

    def list_checkpoints(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            return self.security.list_checkpoints(con, limit=limit)

    def list_signing_keys(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            return self.security.list_keys(con)

    def rotate_signing_key(self) -> dict[str, Any]:
        existing_anchor = self.security.read_anchor()
        if existing_anchor is None:
            raise RuntimeError("External anchor is missing; refusing key rotation")
        with self.connect() as con:
            verification = self.security.verify_checkpoints(con, anchor=existing_anchor)
            if not verification["valid"]:
                raise RuntimeError("Checkpoint verification failed; refusing key rotation")
            result = self.security.rotate_key(con)
            anchor = self.security.anchor_document(
                con, result["checkpoint"]["checkpoint_id"]
            )
        self.security.write_anchor(anchor)
        result["anchor"] = self.security.verify_anchor_document(anchor)
        return result

    def list_audit_branches(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            return self.security.list_branches(con)

    def create_signed_backup(self, label: str | None = None) -> dict[str, Any]:
        checkpoint_result = self.create_signed_checkpoint(reason="backup")
        checkpoint = checkpoint_result["checkpoint"]
        with self.connect() as con:
            anchor = self.security.anchor_document(con, checkpoint["checkpoint_id"])
        return self.backups.create(
            checkpoint=checkpoint,
            anchor=anchor,
            label=label,
        )

    def verify_signed_backup(self, filename: str) -> dict[str, Any]:
        return self.backups.verify(filename)

    def plan_backup_restore(self, filename: str) -> dict[str, Any]:
        with self.connect() as con:
            return self.backups.plan_restore(filename, con)

    def adopt_restore_branch(self, previous_anchor: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as con:
            result = self.security.adopt_restore_branch(con, previous_anchor)
            anchor = self.security.anchor_document(
                con, result["checkpoint"]["checkpoint_id"]
            )
        self.security.write_anchor(anchor)
        result["anchor"] = self.security.verify_anchor_document(anchor)
        return result

    def validate_response_attribution(
        self,
        *,
        content: str,
        conversation_id: str | None = None,
        event_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
        claims: list[dict[str, Any]] | None = None,
        threshold: float = 0.46,
    ) -> dict[str, Any]:
        with self.connect() as con:
            return self.attribution_gate.validate_candidate(
                con,
                content=content,
                conversation_id=conversation_id,
                event_ids=event_ids,
                memory_ids=memory_ids,
                claims=claims,
                threshold=threshold,
            )

    def select_response_candidate(
        self,
        *,
        candidates: list[dict[str, Any]],
        conversation_id: str | None = None,
        event_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
        threshold: float = 0.46,
    ) -> dict[str, Any]:
        with self.connect() as con:
            return self.attribution_gate.select_candidates(
                con,
                candidates=candidates,
                conversation_id=conversation_id,
                event_ids=event_ids,
                memory_ids=memory_ids,
                threshold=threshold,
            )

    def verify_audit(self, verify_objects: bool = True) -> dict[str, Any]:
        with self.connect() as con:
            return self.audit.verify(con, verify_objects=verify_objects)

    def list_audit_events(
        self,
        object_type: str | None = None,
        object_id: str | None = None,
        limit: int = 100,
        include_payload: bool = False,
    ) -> list[dict[str, Any]]:
        with self.connect() as con:
            return self.audit.list_events(
                con,
                object_type=object_type,
                object_id=object_id,
                limit=limit,
                include_payload=include_payload,
            )

    def get_provenance(self, object_type: str, object_id: str) -> dict[str, Any]:
        if object_type not in {"raw_message", "memory_trace", "memory"}:
            raise ValueError(f"Unknown object_type: {object_type}")
        with self.connect() as con:
            return self.audit.provenance(con, object_type, object_id)

    def migrate_legacy_memories(self, con: sqlite3.Connection) -> dict[str, int]:
        counts = {memory_type: 0 for memory_type in LEGACY_MEMORY_TYPES}
        migration_done = con.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
        if migration_done:
            return counts

        table_map = {
            "episodic": "episodic_memories",
            "project": "project_memories",
            "persistent": "persistent_memories",
        }
        for legacy_type, table in table_map.items():
            for row in con.execute(f"SELECT * FROM {table}").fetchall():
                item = self.legacy_row_to_item(legacy_type, row)
                if self.upsert_memory(item, con=con, only_if_missing=True):
                    counts[legacy_type] += 1
        return counts

    def legacy_row_to_item(self, legacy_type: str, row: sqlite3.Row) -> dict[str, Any]:
        if legacy_type == "episodic":
            legacy = {
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "date": row["date"],
                "keywords": loads(row["keywords_json"], []),
                "entities": loads(row["entities_json"], []),
                "emotion": {
                    "valence": row["emotion_valence"],
                    "intensity": row["emotion_intensity"],
                    "tags": loads(row["emotion_tags_json"], []),
                },
                "importance_score": row["importance_score"],
                "recency_score": row["recency_score"],
                "repetition_score": row["repetition_score"],
                "continuity_score": row["continuity_score"],
                "last_recalled_at": row["last_recalled_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "source": loads(row["source_json"], {}),
                "retention": loads(row["retention_json"], {}),
                "confidence": row["confidence"],
                "evidence_type": row["evidence_type"],
                "wording_policy": row["wording_policy"],
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        elif legacy_type == "project":
            legacy = {
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "status": row["status"],
                "current_state": loads(row["current_state_json"], []),
                "open_questions": loads(row["open_questions_json"], []),
                "related_episodes": loads(row["related_episodes_json"], []),
                "keywords": loads(row["keywords_json"], []),
                "importance_score": row["importance_score"],
                "last_recalled_at": row["last_recalled_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "source": loads(row["source_json"], {}),
                "confidence": row["confidence"],
                "evidence_type": row["evidence_type"],
                "wording_policy": row["wording_policy"],
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        else:
            legacy = {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "category": row["category"],
                "keywords": loads(row["keywords_json"], []),
                "importance_score": row["importance_score"],
                "last_recalled_at": row["last_recalled_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "source": loads(row["source_json"], {}),
                "confidence": row["confidence"],
                "evidence_type": row["evidence_type"],
                "wording_policy": row["wording_policy"],
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        return self.legacy_to_memory(legacy_type, legacy)

    def legacy_to_memory(self, legacy_type: str, legacy: dict[str, Any]) -> dict[str, Any]:
        if legacy_type not in LEGACY_TYPE_MAP:
            raise ValueError(f"Unknown legacy memory type: {legacy_type}")

        memory_type = LEGACY_TYPE_MAP[legacy_type]
        category = normalize_text(str(legacy.get("category", "")))
        if legacy_type == "persistent":
            if any(term in category for term in ("procedure", "workflow", "tool", "手順")):
                memory_type = "procedural"
            elif any(term in category for term in ("embodied", "physical", "身体")):
                memory_type = "embodied"

        confirmed = bool(legacy.get("user_confirmed")) or legacy.get("evidence_type") == "explicit"
        importance = clamp(legacy.get("importance_score", 0.5))
        activation = clamp(legacy.get("recency_score", importance))
        if confirmed:
            stability = 1.0
        elif legacy_type == "persistent":
            stability = 0.55
        elif legacy_type == "project":
            stability = 0.40
        else:
            stability = 0.28
        continuity = clamp(legacy.get("continuity_score", 0.6 if legacy_type == "project" else 0.0))
        source = dict(legacy.get("source") or {})
        source_event_ids = self.source_event_ids(source)
        created_at = legacy.get("created_at") or utc_now()
        updated_at = legacy.get("updated_at") or created_at

        metadata = {"legacy_memory_type": legacy_type}
        if legacy_type == "episodic":
            metadata.update(
                {
                    "date": legacy.get("date"),
                    "emotion": legacy.get("emotion") or {},
                    "repetition_score": clamp(legacy.get("repetition_score", 0.0)),
                    "retention": legacy.get("retention") or {},
                    "wording_policy": legacy.get("wording_policy", "tentative"),
                }
            )
        elif legacy_type == "project":
            metadata.update(
                {
                    "status": legacy.get("status", "active"),
                    "current_state": legacy.get("current_state") or [],
                    "open_questions": legacy.get("open_questions") or [],
                    "related_episodes": legacy.get("related_episodes") or [],
                    "wording_policy": legacy.get("wording_policy", "tentative"),
                }
            )
        else:
            metadata.update(
                {
                    "category": legacy.get("category", "preference"),
                    "wording_policy": legacy.get("wording_policy", "confirmed" if confirmed else "tentative"),
                }
            )

        return {
            "id": legacy["id"],
            "memory_type": memory_type,
            "title": legacy.get("title") or "Untitled memory",
            "content": legacy.get("summary") if legacy_type != "persistent" else legacy.get("content", ""),
            "keywords": legacy.get("keywords") or [],
            "entities": legacy.get("entities") or [],
            "acquisition_mode": "user_explicit" if confirmed else "system_derived",
            "epistemic_status": "confirmed" if confirmed else "inferred",
            "epistemic_confidence": clamp(legacy.get("confidence", 1.0 if confirmed else 0.5)),
            "activation": activation,
            "salience": importance,
            "stability": stability,
            "continuity_score": continuity,
            "retention_score": retention_score(activation, importance, stability, continuity),
            "last_recalled_at": legacy.get("last_recalled_at"),
            "pinned": bool(legacy.get("pinned")),
            "archived": bool(legacy.get("archived")),
            "evidence_summary": str(source.get("evidence_summary", "")),
            "source_event_ids": source_event_ids,
            "source": source,
            "metadata": metadata,
            "consolidated_at": created_at,
            "legacy_memory_type": legacy_type,
            "legacy_memory_id": legacy["id"],
            "created_at": created_at,
            "updated_at": updated_at,
        }

    @staticmethod
    def source_event_ids(source: dict[str, Any]) -> list[str]:
        values: list[Any] = []
        for key in ("source_event_ids", "message_ids", "source_message_ids"):
            item = source.get(key)
            values.extend(item if isinstance(item, list) else ([item] if item else []))
        for key in ("message_id", "turn_id", "event_id"):
            if source.get(key):
                values.append(source[key])
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value)
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    def sync_legacy_memory(
        self,
        legacy_type: str,
        legacy: dict[str, Any],
        con: sqlite3.Connection | None = None,
    ) -> str:
        item = self.legacy_to_memory(legacy_type, legacy)
        self.upsert_memory(item, con=con)
        return str(item["id"])

    def upsert_memory(
        self,
        item: dict[str, Any],
        con: sqlite3.Connection | None = None,
        only_if_missing: bool = False,
    ) -> bool:
        own = con is None
        con = con or self.connect()
        try:
            memory_type = str(item.get("memory_type", ""))
            if memory_type not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory_type: {memory_type}")
            acquisition_mode = str(item.get("acquisition_mode", "system_derived"))
            if acquisition_mode not in ACQUISITION_MODES:
                raise ValueError(f"Unknown acquisition_mode: {acquisition_mode}")
            epistemic_status = str(item.get("epistemic_status", "inferred"))
            if epistemic_status not in EPISTEMIC_STATUSES:
                raise ValueError(f"Unknown epistemic_status: {epistemic_status}")

            memory_id = str(item.get("id") or new_id("memory"))
            existing_temporal = con.execute(
                "SELECT * FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if only_if_missing and existing_temporal:
                return False
            now = utc_now()
            activation = clamp(item.get("activation", 0.5))
            salience = clamp(item.get("salience", 0.5))
            stability = clamp(item.get("stability", 0.3))
            continuity = clamp(item.get("continuity_score", 0.0))
            computed_retention = retention_score(activation, salience, stability, continuity)
            created_at = item.get("created_at") or now
            last_decayed_at = item.get("last_decayed_at") or item.get("updated_at") or created_at

            con.execute(
                """
                INSERT INTO memories (
                    id, memory_type, title, content, keywords_json, entities_json,
                    acquisition_mode, epistemic_status, epistemic_confidence,
                    activation, salience, stability, continuity_score, retention_score,
                    last_recalled_at, recall_count, last_decayed_at, expires_at,
                    pinned, archived, evidence_summary, source_event_ids_json,
                    source_json, metadata_json, observation_statement, perspective,
                    evidence_kind, observation_fidelity, source_reliability,
                    world_hypothesis, consolidated_at, legacy_memory_type,
                    legacy_memory_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    memory_type=excluded.memory_type,
                    title=excluded.title,
                    content=excluded.content,
                    keywords_json=excluded.keywords_json,
                    entities_json=excluded.entities_json,
                    acquisition_mode=excluded.acquisition_mode,
                    epistemic_status=excluded.epistemic_status,
                    epistemic_confidence=excluded.epistemic_confidence,
                    activation=MAX(memories.activation, excluded.activation),
                    salience=excluded.salience,
                    stability=MAX(memories.stability, excluded.stability),
                    continuity_score=excluded.continuity_score,
                    retention_score=MAX(memories.retention_score, excluded.retention_score),
                    pinned=excluded.pinned,
                    archived=excluded.archived,
                    evidence_summary=excluded.evidence_summary,
                    source_event_ids_json=excluded.source_event_ids_json,
                    source_json=excluded.source_json,
                    metadata_json=excluded.metadata_json,
                    observation_statement=excluded.observation_statement,
                    perspective=excluded.perspective,
                    evidence_kind=excluded.evidence_kind,
                    observation_fidelity=excluded.observation_fidelity,
                    source_reliability=excluded.source_reliability,
                    world_hypothesis=excluded.world_hypothesis,
                    legacy_memory_type=excluded.legacy_memory_type,
                    legacy_memory_id=excluded.legacy_memory_id,
                    updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    memory_type,
                    str(item.get("title") or "Untitled memory"),
                    str(item.get("content") or ""),
                    dumps(item.get("keywords") or []),
                    dumps(item.get("entities") or []),
                    acquisition_mode,
                    epistemic_status,
                    clamp(item.get("epistemic_confidence", 0.5)),
                    activation,
                    salience,
                    stability,
                    continuity,
                    computed_retention,
                    item.get("last_recalled_at"),
                    int(item.get("recall_count", 0)),
                    last_decayed_at,
                    item.get("expires_at"),
                    1 if item.get("pinned") else 0,
                    1 if item.get("archived") else 0,
                    str(item.get("evidence_summary") or ""),
                    dumps(item.get("source_event_ids") or []),
                    dumps(item.get("source") or {}),
                    dumps(item.get("metadata") or {}),
                    item.get("observation_statement"),
                    item.get("perspective"),
                    item.get("evidence_kind"),
                    _as_optional_score(item.get("observation_fidelity")),
                    _as_optional_score(item.get("source_reliability")),
                    item.get("world_hypothesis"),
                    item.get("consolidated_at") or now,
                    item.get("legacy_memory_type"),
                    item.get("legacy_memory_id"),
                    created_at,
                    item.get("updated_at") or now,
                ),
            )
            temporal_item = dict(item)
            if existing_temporal:
                for key in existing_temporal.keys():
                    if temporal_item.get(key) is None:
                        temporal_item[key] = existing_temporal[key]
                if temporal_item.get("derived_from") is None and "derived_from_json" in existing_temporal.keys():
                    temporal_item["derived_from"] = loads(existing_temporal["derived_from_json"], [])
            temporal = temporal_values(
                temporal_item,
                created_at=created_at,
                persisted_at=now,
                default_time_source="derived",
            )
            provenance = provenance_values(
                temporal_item,
                default_actor_role="user" if acquisition_mode == "user_explicit" else "system",
                default_source_channel="explicit_memory" if acquisition_mode == "user_explicit" else "internal",
                default_content_origin="original" if acquisition_mode == "user_explicit" else "derived",
            )
            con.execute(
                """
                UPDATE memories
                SET event_time=?, received_at=?, persisted_at=?, source_time=?,
                    timezone=?, time_source=?, ingest_delay_seconds=?, valid_from=?,
                    valid_until=?, superseded_by=?, actor_id=?, actor_role=?,
                    source_channel=?, content_origin=?, extractor=?, derived_from_json=?
                WHERE id=?
                """,
                (
                    *temporal.values(),
                    provenance["actor_id"],
                    provenance["actor_role"],
                    provenance["source_channel"],
                    provenance["content_origin"],
                    provenance["extractor"],
                    dumps(provenance["derived_from"]),
                    memory_id,
                ),
            )
            self.index_memory(con, memory_id)
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory.updated" if existing_temporal else "memory.created",
                    object_type="memory",
                    object_id=memory_id,
                    actor_id=provenance["actor_id"],
                    actor_role=provenance["actor_role"],
                    source_channel=provenance["source_channel"],
                    content_origin=provenance["content_origin"],
                    extractor=provenance["extractor"],
                    derivations=provenance["derived_from"],
                    event_time=temporal["event_time"],
                    received_at=temporal["received_at"],
                    integrity_tier="durable",
                    payload={"acquisition_mode": acquisition_mode},
                )
            if own:
                con.commit()
            return True
        finally:
            if own:
                con.close()

    def create_trace(self, item: dict[str, Any]) -> dict[str, Any]:
        trace_stage = str(item.get("trace_stage", "proto"))
        if trace_stage not in TRACE_STAGES:
            raise ValueError(f"Unknown trace_stage: {trace_stage}")
        candidate_memory_type = item.get("candidate_memory_type")
        if candidate_memory_type is not None and candidate_memory_type not in MEMORY_TYPES:
            raise ValueError(f"Unknown candidate_memory_type: {candidate_memory_type}")
        acquisition_mode = str(item.get("acquisition_mode", "automatic"))
        epistemic_status = str(item.get("epistemic_status", "inferred"))
        if acquisition_mode not in ACQUISITION_MODES:
            raise ValueError(f"Unknown acquisition_mode: {acquisition_mode}")
        if epistemic_status not in EPISTEMIC_STATUSES:
            raise ValueError(f"Unknown epistemic_status: {epistemic_status}")
        content = str(item.get("content") or "").strip()
        if not content:
            raise ValueError("content is required.")

        trace_id = str(item.get("id") or new_id("trace"))
        now = utc_now()
        activation = clamp(item.get("activation", 0.5))
        salience = clamp(item.get("salience", 0.5))
        stability = clamp(item.get("stability", 0.1))
        continuity = clamp(item.get("continuity_score", 0.0))
        score = retention_score(activation, salience, stability, continuity)
        record_threshold = clamp(item.get("record_threshold", DEFAULT_RECORD_THRESHOLD))
        review_threshold = clamp(item.get("review_threshold", DEFAULT_REVIEW_THRESHOLD))
        delete_threshold = clamp(item.get("delete_threshold", DEFAULT_DELETE_THRESHOLD))
        if not delete_threshold < record_threshold <= review_threshold:
            raise ValueError("Thresholds must satisfy delete < record <= review.")

        with self.connect() as con:
            con.execute(
                """
                INSERT INTO memory_traces (
                    id, conversation_id, turn_id, trace_stage, candidate_memory_type,
                    title, content, keywords_json, acquisition_mode, epistemic_status,
                    epistemic_confidence, activation, salience, stability,
                    continuity_score, retention_score, affect_signal_json,
                    evidence_summary, source_event_ids_json, source_json,
                    observation_statement, perspective, evidence_kind,
                    observation_fidelity, source_reliability, world_hypothesis,
                    record_threshold, review_threshold, delete_threshold, status,
                    last_recalled_at, recall_count, last_decayed_at, expires_at,
                    consolidated_at, consolidated_memory_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trace_id,
                    item.get("conversation_id"),
                    item.get("turn_id"),
                    trace_stage,
                    candidate_memory_type,
                    str(item.get("title") or content[:80]),
                    content,
                    dumps(item.get("keywords") or query_terms(content)),
                    acquisition_mode,
                    epistemic_status,
                    clamp(item.get("epistemic_confidence", 0.5)),
                    activation,
                    salience,
                    stability,
                    continuity,
                    score,
                    dumps(item.get("affect_signal") or {}),
                    str(item.get("evidence_summary") or ""),
                    dumps(item.get("source_event_ids") or []),
                    dumps(item.get("source") or {}),
                    item.get("observation_statement"),
                    item.get("perspective"),
                    item.get("evidence_kind"),
                    _as_optional_score(item.get("observation_fidelity")),
                    _as_optional_score(item.get("source_reliability")),
                    item.get("world_hypothesis"),
                    record_threshold,
                    review_threshold,
                    delete_threshold,
                    str(item.get("status", "active")),
                    item.get("last_recalled_at"),
                    int(item.get("recall_count", 0)),
                    item.get("last_decayed_at") or now,
                    item.get("expires_at"),
                    item.get("consolidated_at"),
                    item.get("consolidated_memory_id"),
                    item.get("created_at") or now,
                    now,
                ),
            )
            observed_at = item.get("observed_at") or item.get("created_at") or now
            con.execute(
                """
                UPDATE memory_traces
                SET capture_score=?, repetition_score=?, unfinished_score=?,
                    confirmation_score=?, occurrence_count=?, extraction_reasons_json=?,
                    content_fingerprint=?, first_observed_at=?, last_observed_at=?
                WHERE id=?
                """,
                (
                    clamp(item.get("capture_score", 0.0)),
                    clamp(item.get("repetition_score", 0.0)),
                    clamp(item.get("unfinished_score", 0.0)),
                    clamp(item.get("confirmation_score", 0.0)),
                    max(1, int(item.get("occurrence_count", 1))),
                    dumps(item.get("extraction_reasons") or []),
                    str(item.get("content_fingerprint") or ""),
                    item.get("first_observed_at") or observed_at,
                    item.get("last_observed_at") or observed_at,
                    trace_id,
                ),
            )
            temporal = temporal_values(
                item,
                created_at=item.get("created_at") or now,
                persisted_at=now,
                default_time_source="automatic_capture",
            )
            con.execute(
                """
                UPDATE memory_traces
                SET event_time=?, received_at=?, persisted_at=?, source_time=?,
                    timezone=?, time_source=?, ingest_delay_seconds=?, valid_from=?,
                    valid_until=?, superseded_by=?
                WHERE id=?
                """,
                (*temporal.values(), trace_id),
            )
            provenance = provenance_values(
                item,
                default_actor_role="system",
                default_source_channel="automatic_capture",
                default_content_origin="summary",
            )
            con.execute(
                """
                UPDATE memory_traces
                SET actor_id=?, actor_role=?, source_channel=?, content_origin=?,
                    extractor=?, derived_from_json=?
                WHERE id=?
                """,
                (
                    provenance["actor_id"],
                    provenance["actor_role"],
                    provenance["source_channel"],
                    provenance["content_origin"],
                    provenance["extractor"],
                    dumps(provenance["derived_from"]),
                    trace_id,
                ),
            )
            self.index_trace(con, trace_id)
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory_trace.created",
                    object_type="memory_trace",
                    object_id=trace_id,
                    actor_id=provenance["actor_id"],
                    actor_role=provenance["actor_role"],
                    source_channel=provenance["source_channel"],
                    content_origin=provenance["content_origin"],
                    extractor=provenance["extractor"],
                    derivations=provenance["derived_from"],
                    event_time=temporal["event_time"],
                    received_at=temporal["received_at"],
                    integrity_tier="routine",
                    payload={"trace_stage": trace_stage},
                )
        return self.get_trace(trace_id)

    def get_trace(self, trace_id: str, con: sqlite3.Connection | None = None) -> dict[str, Any]:
        own = con is None
        con = con or self.connect()
        try:
            row = con.execute("SELECT * FROM memory_traces WHERE id=?", (trace_id,)).fetchone()
            if row is None:
                raise KeyError(f"Memory trace not found: {trace_id}")
            return self.trace_row(row)
        finally:
            if own:
                con.close()

    def list_traces(
        self,
        status: str | None = None,
        conversation_id: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            if status not in TRACE_STATUSES:
                raise ValueError(f"Unknown trace status: {status}")
            clauses.append("status=?")
            params.append(status)
        elif not include_archived:
            clauses.append("status!='archived'")
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM memory_traces {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self.trace_row(row) for row in rows]

    def patch_trace(self, trace_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "trace_stage",
            "candidate_memory_type",
            "title",
            "content",
            "keywords",
            "acquisition_mode",
            "epistemic_status",
            "epistemic_confidence",
            "activation",
            "salience",
            "stability",
            "continuity_score",
            "affect_signal",
            "capture_score",
            "repetition_score",
            "unfinished_score",
            "confirmation_score",
            "occurrence_count",
            "extraction_reasons",
            "content_fingerprint",
            "first_observed_at",
            "last_observed_at",
            "event_time",
            "received_at",
            "persisted_at",
            "source_time",
            "timezone",
            "time_source",
            "valid_from",
            "valid_until",
            "superseded_by",
            "actor_id",
            "actor_role",
            "source_channel",
            "content_origin",
            "extractor",
            "derived_from",
            "evidence_summary",
            "source_event_ids",
            "source",
            "observation_statement",
            "perspective",
            "evidence_kind",
            "observation_fidelity",
            "source_reliability",
            "world_hypothesis",
            "record_threshold",
            "review_threshold",
            "delete_threshold",
            "status",
            "expires_at",
        }
        updates = {key: value for key, value in patch.items() if key in allowed}
        if not updates:
            raise ValueError("Patch has no editable fields.")
        trace = self.get_trace(trace_id)
        merged = {**trace, **updates}
        if merged.get("candidate_memory_type") is not None and merged["candidate_memory_type"] not in MEMORY_TYPES:
            raise ValueError(f"Unknown candidate_memory_type: {merged['candidate_memory_type']}")
        if merged["trace_stage"] not in TRACE_STAGES or merged["status"] not in TRACE_STATUSES:
            raise ValueError("Invalid trace_stage or status.")
        if merged["acquisition_mode"] not in ACQUISITION_MODES or merged["epistemic_status"] not in EPISTEMIC_STATUSES:
            raise ValueError("Invalid acquisition_mode or epistemic_status.")
        if not merged["delete_threshold"] < merged["record_threshold"] <= merged["review_threshold"]:
            raise ValueError("Thresholds must satisfy delete < record <= review.")

        activation = clamp(merged["activation"])
        salience = clamp(merged["salience"])
        stability = clamp(merged["stability"])
        continuity = clamp(merged["continuity_score"])
        temporal = temporal_values(
            merged,
            created_at=merged["created_at"],
            persisted_at=merged.get("persisted_at") or merged["created_at"],
            default_time_source="automatic_capture",
        )
        provenance = provenance_values(
            merged,
            default_actor_role="system",
            default_source_channel="automatic_capture",
            default_content_origin="summary",
        )
        db_values = {
            "trace_stage": merged["trace_stage"],
            "candidate_memory_type": merged.get("candidate_memory_type"),
            "title": str(merged["title"]),
            "content": str(merged["content"]),
            "keywords_json": dumps(merged.get("keywords") or []),
            "acquisition_mode": merged["acquisition_mode"],
            "epistemic_status": merged["epistemic_status"],
            "epistemic_confidence": clamp(merged["epistemic_confidence"]),
            "activation": activation,
            "salience": salience,
            "stability": stability,
            "continuity_score": continuity,
            "retention_score": retention_score(activation, salience, stability, continuity),
            "affect_signal_json": dumps(merged.get("affect_signal") or {}),
            "capture_score": clamp(merged.get("capture_score", 0.0)),
            "repetition_score": clamp(merged.get("repetition_score", 0.0)),
            "unfinished_score": clamp(merged.get("unfinished_score", 0.0)),
            "confirmation_score": clamp(merged.get("confirmation_score", 0.0)),
            "occurrence_count": max(1, int(merged.get("occurrence_count", 1))),
            "extraction_reasons_json": dumps(merged.get("extraction_reasons") or []),
            "content_fingerprint": str(merged.get("content_fingerprint") or ""),
            "first_observed_at": merged.get("first_observed_at"),
            "last_observed_at": merged.get("last_observed_at"),
            **temporal,
            "actor_id": provenance["actor_id"],
            "actor_role": provenance["actor_role"],
            "source_channel": provenance["source_channel"],
            "content_origin": provenance["content_origin"],
            "extractor": provenance["extractor"],
            "derived_from_json": dumps(provenance["derived_from"]),
            "evidence_summary": str(merged.get("evidence_summary") or ""),
            "source_event_ids_json": dumps(merged.get("source_event_ids") or []),
            "source_json": dumps(merged.get("source") or {}),
            "observation_statement": merged.get("observation_statement"),
            "perspective": merged.get("perspective"),
            "evidence_kind": merged.get("evidence_kind"),
            "observation_fidelity": _as_optional_score(merged.get("observation_fidelity")),
            "source_reliability": _as_optional_score(merged.get("source_reliability")),
            "world_hypothesis": merged.get("world_hypothesis"),
            "record_threshold": clamp(merged["record_threshold"]),
            "review_threshold": clamp(merged["review_threshold"]),
            "delete_threshold": clamp(merged["delete_threshold"]),
            "status": merged["status"],
            "expires_at": merged.get("expires_at"),
            "updated_at": utc_now(),
        }
        assignments = ", ".join(f"{key}=?" for key in db_values)
        with self.connect() as con:
            con.execute(f"UPDATE memory_traces SET {assignments} WHERE id=?", (*db_values.values(), trace_id))
            self.index_trace(con, trace_id)
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory_trace.updated",
                    object_type="memory_trace",
                    object_id=trace_id,
                    actor_id=provenance["actor_id"],
                    actor_role=provenance["actor_role"],
                    source_channel=provenance["source_channel"],
                    content_origin=provenance["content_origin"],
                    extractor=provenance["extractor"],
                    derivations=provenance["derived_from"],
                    event_time=temporal["event_time"],
                    received_at=temporal["received_at"],
                    integrity_tier="durable" if merged["epistemic_status"] == "confirmed" else "routine",
                    payload={"changed_fields": sorted(updates)},
                )
        return self.get_trace(trace_id)

    def recall_trace(self, trace_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.connect() as con:
            trace = self.get_trace(trace_id, con=con)
            previous = parse_time(trace.get("last_recalled_at"), parse_time(trace["created_at"]))
            spaced = (now - previous).total_seconds() >= 86400
            activation = clamp(trace["activation"] + DEFAULT_RECALL_BOOST * (1.0 - trace["activation"]))
            stability = clamp(trace["stability"] + (DEFAULT_SPACED_STABILITY_BOOST if spaced else 0.0))
            score = retention_score(activation, trace["salience"], stability, trace["continuity_score"])
            con.execute(
                """
                UPDATE memory_traces
                SET activation=?, stability=?, retention_score=?, last_recalled_at=?,
                    recall_count=recall_count+1, updated_at=?
                WHERE id=?
                """,
                (activation, stability, score, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), trace_id),
            )
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory_trace.recalled",
                    object_type="memory_trace",
                    object_id=trace_id,
                    actor_role="system",
                    source_channel="retrieval",
                    content_origin="derived",
                    integrity_tier="routine",
                    payload={"spaced": spaced},
                )
        return self.get_trace(trace_id)

    def review_trace(
        self,
        trace_id: str,
        decision: str,
        memory_type: str | None = None,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"confirm", "keep", "archive"}:
            raise ValueError("decision must be confirm, keep, or archive.")

        trace = self.get_trace(trace_id)
        if trace["status"] == "consolidated":
            if decision != "confirm":
                raise ValueError("Consolidated traces can only repeat the confirm decision.")
            memory_id = trace.get("consolidated_memory_id")
            return {
                "decision": "confirm",
                "trace": trace,
                "memory": self.get_memory(memory_id) if memory_id else None,
            }

        evidence_summary = trace.get("evidence_summary", "")
        if notes:
            evidence_summary = "\n".join(part for part in (evidence_summary, notes.strip()) if part)

        if decision == "confirm":
            reviewed = self.patch_trace(
                trace_id,
                {
                    "trace_stage": "candidate",
                    "acquisition_mode": "reviewed",
                    "epistemic_status": "confirmed",
                    "status": "review",
                    "evidence_summary": evidence_summary,
                },
            )
            memory = self.consolidate_trace(
                trace_id,
                memory_type=memory_type,
                title=title,
                confirmed=True,
            )
            return {
                "decision": decision,
                "trace": self.get_trace(reviewed["id"]),
                "memory": memory,
            }

        status = "active" if decision == "keep" else "archived"
        reviewed = self.patch_trace(
            trace_id,
            {
                "trace_stage": "candidate",
                "acquisition_mode": "reviewed",
                "status": status,
                "evidence_summary": evidence_summary,
            },
        )
        return {"decision": decision, "trace": reviewed, "memory": None}

    def consolidate_trace(
        self,
        trace_id: str,
        memory_type: str | None = None,
        title: str | None = None,
        confirmed: bool = False,
        con: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        own = con is None
        con = con or self.connect()
        try:
            trace = self.get_trace(trace_id, con=con)
            if trace["status"] == "consolidated" and trace.get("consolidated_memory_id"):
                return self.get_memory(trace["consolidated_memory_id"], con=con)
            if trace["status"] == "archived":
                raise ValueError("Archived traces cannot be consolidated.")
            target_type = memory_type or trace.get("candidate_memory_type")
            if target_type not in MEMORY_TYPES:
                raise ValueError("A valid memory_type is required for consolidation.")

            is_confirmed = confirmed or trace["acquisition_mode"] == "user_explicit" or trace["epistemic_status"] == "confirmed"
            now = utc_now()
            memory_id = new_id("memory")
            memory_item = {
                "id": memory_id,
                "memory_type": target_type,
                "title": title or trace["title"],
                "content": trace["content"],
                "keywords": trace["keywords"],
                "acquisition_mode": "reviewed" if confirmed and trace["acquisition_mode"] != "user_explicit" else trace["acquisition_mode"],
                "epistemic_status": "confirmed" if is_confirmed else "inferred",
                "epistemic_confidence": 1.0 if trace["acquisition_mode"] == "user_explicit" else trace["epistemic_confidence"],
                "activation": trace["activation"],
                "salience": trace["salience"],
                "stability": max(trace["stability"], 0.35 if not is_confirmed else 0.9),
                "continuity_score": trace["continuity_score"],
                "pinned": is_confirmed,
                "evidence_summary": trace["evidence_summary"],
                "source_event_ids": trace["source_event_ids"],
                "source": trace["source"],
                "metadata": {"origin_trace_id": trace_id, "affect_signal": trace["affect_signal"]},
                "observation_statement": trace.get("observation_statement"),
                "perspective": trace.get("perspective"),
                "evidence_kind": trace.get("evidence_kind"),
                "observation_fidelity": trace.get("observation_fidelity"),
                "source_reliability": trace.get("source_reliability"),
                "world_hypothesis": trace.get("world_hypothesis"),
                "event_time": trace.get("event_time"),
                "received_at": trace.get("received_at"),
                "source_time": trace.get("source_time"),
                "timezone": trace.get("timezone"),
                "time_source": "trace_consolidation",
                "valid_from": trace.get("valid_from"),
                "valid_until": trace.get("valid_until"),
                "superseded_by": trace.get("superseded_by"),
                "actor_id": trace.get("actor_id"),
                "actor_role": "system",
                "source_channel": "trace_consolidation",
                "content_origin": "derived",
                "extractor": trace.get("extractor"),
                "derived_from": [
                    {
                        "object_type": "memory_trace",
                        "object_id": trace_id,
                        "relation": "consolidated_from",
                    },
                    *[
                        {
                            "object_type": "raw_message",
                            "object_id": source_event_id,
                            "relation": "supported_by",
                        }
                        for source_event_id in trace["source_event_ids"]
                    ],
                ],
                "consolidated_at": now,
            }
            self.upsert_memory(memory_item, con=con)
            con.execute(
                "INSERT INTO memory_evidence_links(id, memory_id, trace_id, relation, created_at) VALUES (?,?,?,?,?)",
                (new_id("evidence"), memory_id, trace_id, "consolidated_from", now),
            )
            for source_event_id in trace["source_event_ids"]:
                con.execute(
                    "INSERT INTO memory_evidence_links(id, memory_id, source_event_id, relation, created_at) VALUES (?,?,?,?,?)",
                    (new_id("evidence"), memory_id, source_event_id, "supported_by", now),
                )
            con.execute(
                """
                UPDATE memory_traces
                SET status='consolidated', consolidated_at=?, consolidated_memory_id=?, updated_at=?
                WHERE id=?
                """,
                (now, memory_id, now, trace_id),
            )
            con.execute("DELETE FROM memory_trace_fts WHERE trace_id=?", (trace_id,))
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory_trace.consolidated",
                    object_type="memory_trace",
                    object_id=trace_id,
                    actor_id=trace.get("actor_id"),
                    actor_role="system",
                    source_channel="trace_consolidation",
                    content_origin="derived",
                    extractor=trace.get("extractor"),
                    derivations=[
                        {
                            "object_type": "memory",
                            "object_id": memory_id,
                            "relation": "consolidated_into",
                        }
                    ],
                    event_time=now,
                    received_at=now,
                    integrity_tier="durable",
                    payload={"memory_id": memory_id, "confirmed": is_confirmed},
                )
            if own:
                con.commit()
            return self.get_memory(memory_id, con=con)
        finally:
            if own:
                con.close()

    def run_maintenance(
        self,
        as_of: str | None = None,
        daily_decay_rate: float = DEFAULT_DAILY_DECAY_RATE,
        auto_consolidate: bool = False,
        archive_below_threshold: bool = True,
    ) -> dict[str, Any]:
        target_time = parse_time(as_of)
        target_text = target_time.isoformat(timespec="seconds")
        report: dict[str, Any] = {
            "as_of": target_text,
            "traces_decayed": 0,
            "traces_archived": 0,
            "traces_review": 0,
            "traces_consolidated": 0,
            "memories_decayed": 0,
            "memories_archived": 0,
            "consolidated_memory_ids": [],
        }
        rate = clamp(daily_decay_rate, 0.01, 0.999)
        with self.connect() as con:
            trace_rows = con.execute(
                "SELECT * FROM memory_traces WHERE status IN ('active','review')"
            ).fetchall()
            for row in trace_rows:
                trace = self.trace_row(row)
                previous = parse_time(trace["last_decayed_at"], parse_time(trace["created_at"]))
                elapsed_days = max(0.0, (target_time - previous).total_seconds() / 86400.0)
                if elapsed_days <= 0:
                    continue
                activation = decay_activation(trace["activation"], elapsed_days, trace["salience"], trace["stability"], rate)
                score = retention_score(activation, trace["salience"], trace["stability"], trace["continuity_score"])
                status = "active"
                expired = bool(trace.get("expires_at") and parse_time(trace["expires_at"]) <= target_time)
                if archive_below_threshold and (expired or score < trace["delete_threshold"]):
                    status = "archived"
                    report["traces_archived"] += 1
                elif score >= trace["review_threshold"]:
                    status = "review"
                    report["traces_review"] += 1
                con.execute(
                    "UPDATE memory_traces SET activation=?, retention_score=?, status=?, last_decayed_at=?, updated_at=? WHERE id=?",
                    (activation, score, status, target_text, target_text, trace["id"]),
                )
                if self.audit_enabled(con):
                    self.audit.append_object_event(
                        con,
                        event_type="memory_trace.maintained",
                        object_type="memory_trace",
                        object_id=trace["id"],
                        actor_role="system",
                        source_channel="maintenance",
                        content_origin="derived",
                        derivations=trace.get("derived_from") or [],
                        event_time=target_text,
                        received_at=utc_now(),
                        integrity_tier="routine",
                        payload={"elapsed_days": elapsed_days, "status": status},
                    )
                report["traces_decayed"] += 1
                if status == "archived":
                    con.execute("DELETE FROM memory_trace_fts WHERE trace_id=?", (trace["id"],))
                elif auto_consolidate and score >= trace["record_threshold"] and trace.get("candidate_memory_type"):
                    memory = self.consolidate_trace(trace["id"], con=con)
                    report["traces_consolidated"] += 1
                    report["consolidated_memory_ids"].append(memory["id"])

            memory_rows = con.execute("SELECT * FROM memories WHERE archived=0").fetchall()
            for row in memory_rows:
                memory = self.memory_row(row)
                previous = parse_time(memory["last_decayed_at"], parse_time(memory["created_at"]))
                elapsed_days = max(0.0, (target_time - previous).total_seconds() / 86400.0)
                if elapsed_days <= 0:
                    continue
                activation = decay_activation(memory["activation"], elapsed_days, memory["salience"], memory["stability"], rate)
                score = retention_score(activation, memory["salience"], memory["stability"], memory["continuity_score"])
                archive = (
                    archive_below_threshold
                    and not memory["pinned"]
                    and memory["epistemic_status"] != "confirmed"
                    and score < DEFAULT_DELETE_THRESHOLD
                )
                con.execute(
                    "UPDATE memories SET activation=?, retention_score=?, last_decayed_at=?, archived=?, updated_at=? WHERE id=?",
                    (activation, score, target_text, 1 if archive else 0, target_text, memory["id"]),
                )
                if self.audit_enabled(con):
                    self.audit.append_object_event(
                        con,
                        event_type="memory.maintained",
                        object_type="memory",
                        object_id=memory["id"],
                        actor_role="system",
                        source_channel="maintenance",
                        content_origin="derived",
                        derivations=memory.get("derived_from") or [],
                        event_time=target_text,
                        received_at=utc_now(),
                        integrity_tier="routine",
                        payload={"elapsed_days": elapsed_days, "archived": archive},
                    )
                report["memories_decayed"] += 1
                if archive:
                    report["memories_archived"] += 1
                    con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory["id"],))
        return report

    def get_memory(self, memory_id: str, con: sqlite3.Connection | None = None) -> dict[str, Any]:
        own = con is None
        con = con or self.connect()
        try:
            row = con.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(f"Long-term memory not found: {memory_id}")
            return self.memory_row(row)
        finally:
            if own:
                con.close()

    def list_evidence_links(self, memory_id: str) -> dict[str, Any]:
        with self.connect() as con:
            memory = self.get_memory(memory_id, con=con)
            rows = con.execute(
                """
                SELECT id, memory_id, trace_id, source_event_id, relation, created_at
                FROM memory_evidence_links
                WHERE memory_id=?
                ORDER BY created_at, id
                """,
                (memory_id,),
            ).fetchall()
        return {
            "memory_id": memory_id,
            "source_event_ids": memory["source_event_ids"],
            "links": [dict(row) for row in rows],
        }

    def list_memories(
        self,
        memory_type: str | None = None,
        epistemic_status: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
        temporal_scope: str = "current",
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if memory_type:
            if memory_type not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory_type: {memory_type}")
            clauses.append("memory_type=?")
            params.append(memory_type)
        if epistemic_status:
            if epistemic_status not in EPISTEMIC_STATUSES:
                raise ValueError(f"Unknown epistemic_status: {epistemic_status}")
            clauses.append("epistemic_status=?")
            params.append(epistemic_status)
        if not include_archived:
            clauses.append("archived=0")
        reference = normalize_timestamp(as_of or utc_now(), field_name="as_of")
        self.add_temporal_scope(clauses, params, temporal_scope, reference)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM memories {where} ORDER BY pinned DESC, retention_score DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        memories = [self.memory_row(row) for row in rows]
        for memory in memories:
            memory["temporal_status"] = temporal_state(memory, reference)
        return memories

    @staticmethod
    def add_temporal_scope(
        clauses: list[str],
        params: list[Any],
        temporal_scope: str,
        as_of: str,
    ) -> None:
        if temporal_scope not in TEMPORAL_SCOPES:
            raise ValueError(f"Unknown temporal_scope: {temporal_scope}")
        if temporal_scope == "current":
            clauses.extend(
                [
                    "(valid_from IS NULL OR valid_from<=?)",
                    "(valid_until IS NULL OR valid_until>?)",
                    "(expires_at IS NULL OR expires_at>?)",
                ]
            )
            params.extend([as_of, as_of, as_of])
        elif temporal_scope == "historical":
            clauses.append(
                "((valid_until IS NOT NULL AND valid_until<=?) "
                "OR (expires_at IS NOT NULL AND expires_at<=?) "
                "OR (superseded_by IS NOT NULL AND valid_until IS NULL))"
            )
            params.extend([as_of, as_of])
        elif temporal_scope == "future":
            clauses.extend(["valid_from>?", "superseded_by IS NULL"])
            params.append(as_of)

    def patch_memory(self, memory_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "memory_type",
            "title",
            "content",
            "keywords",
            "entities",
            "acquisition_mode",
            "epistemic_status",
            "epistemic_confidence",
            "activation",
            "salience",
            "stability",
            "continuity_score",
            "pinned",
            "archived",
            "evidence_summary",
            "source_event_ids",
            "source",
            "metadata",
            "observation_statement",
            "perspective",
            "evidence_kind",
            "observation_fidelity",
            "source_reliability",
            "world_hypothesis",
            "expires_at",
            "event_time",
            "received_at",
            "persisted_at",
            "source_time",
            "timezone",
            "time_source",
            "valid_from",
            "valid_until",
            "superseded_by",
            "actor_id",
            "actor_role",
            "source_channel",
            "content_origin",
            "extractor",
            "derived_from",
        }
        updates = {key: value for key, value in patch.items() if key in allowed}
        if not updates:
            raise ValueError("Patch has no editable fields.")
        memory = self.get_memory(memory_id)
        merged = {**memory, **updates}
        if merged["memory_type"] not in MEMORY_TYPES:
            raise ValueError(f"Unknown memory_type: {merged['memory_type']}")
        if merged["acquisition_mode"] not in ACQUISITION_MODES or merged["epistemic_status"] not in EPISTEMIC_STATUSES:
            raise ValueError("Invalid acquisition_mode or epistemic_status.")

        activation = clamp(merged["activation"])
        salience = clamp(merged["salience"])
        stability = clamp(merged["stability"])
        continuity = clamp(merged["continuity_score"])
        temporal = temporal_values(
            merged,
            created_at=merged["created_at"],
            persisted_at=merged.get("persisted_at") or merged["created_at"],
            default_time_source="derived",
        )
        provenance = provenance_values(
            merged,
            default_actor_role="user" if merged["acquisition_mode"] == "user_explicit" else "system",
            default_source_channel="memory_api",
            default_content_origin="original" if merged["acquisition_mode"] == "user_explicit" else "derived",
        )
        values = {
            "memory_type": merged["memory_type"],
            "title": str(merged["title"]),
            "content": str(merged["content"]),
            "keywords_json": dumps(merged.get("keywords") or []),
            "entities_json": dumps(merged.get("entities") or []),
            "acquisition_mode": merged["acquisition_mode"],
            "epistemic_status": merged["epistemic_status"],
            "epistemic_confidence": clamp(merged["epistemic_confidence"]),
            "activation": activation,
            "salience": salience,
            "stability": stability,
            "continuity_score": continuity,
            "retention_score": retention_score(activation, salience, stability, continuity),
            "pinned": 1 if merged.get("pinned") else 0,
            "archived": 1 if merged.get("archived") else 0,
            "evidence_summary": str(merged.get("evidence_summary") or ""),
            "source_event_ids_json": dumps(merged.get("source_event_ids") or []),
            "source_json": dumps(merged.get("source") or {}),
            "metadata_json": dumps(merged.get("metadata") or {}),
            "observation_statement": merged.get("observation_statement"),
            "perspective": merged.get("perspective"),
            "evidence_kind": merged.get("evidence_kind"),
            "observation_fidelity": _as_optional_score(merged.get("observation_fidelity")),
            "source_reliability": _as_optional_score(merged.get("source_reliability")),
            "world_hypothesis": merged.get("world_hypothesis"),
            "expires_at": merged.get("expires_at"),
            **temporal,
            "actor_id": provenance["actor_id"],
            "actor_role": provenance["actor_role"],
            "source_channel": provenance["source_channel"],
            "content_origin": provenance["content_origin"],
            "extractor": provenance["extractor"],
            "derived_from_json": dumps(provenance["derived_from"]),
            "updated_at": utc_now(),
        }
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connect() as con:
            con.execute(f"UPDATE memories SET {assignments} WHERE id=?", (*values.values(), memory_id))
            if merged.get("archived"):
                con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory_id,))
            else:
                self.index_memory(con, memory_id)
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory.updated",
                    object_type="memory",
                    object_id=memory_id,
                    actor_id=provenance["actor_id"],
                    actor_role=provenance["actor_role"],
                    source_channel=provenance["source_channel"],
                    content_origin=provenance["content_origin"],
                    extractor=provenance["extractor"],
                    derivations=provenance["derived_from"],
                    event_time=temporal["event_time"],
                    received_at=temporal["received_at"],
                    integrity_tier="durable",
                    payload={"changed_fields": sorted(updates)},
                )
        return self.get_memory(memory_id)

    def forget_memory(self, memory_id: str) -> None:
        with self.connect() as con:
            exists = con.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not exists:
                raise KeyError(f"Long-term memory not found: {memory_id}")
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory.forgotten",
                    object_type="memory",
                    object_id=memory_id,
                    actor_role="system",
                    source_channel="memory_api",
                    content_origin="derived",
                    derivations=loads(exists["derived_from_json"], []),
                    integrity_tier="durable",
                    payload={"deletion_requested": True},
                )
            con.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory_id,))

    def supersede_memory(
        self,
        memory_id: str,
        replacement_memory_id: str,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        if memory_id == replacement_memory_id:
            raise ValueError("A memory cannot supersede itself.")
        effective = normalize_timestamp(effective_at or utc_now(), field_name="effective_at")
        with self.connect() as con:
            current = self.get_memory(memory_id, con=con)
            replacement = self.get_memory(replacement_memory_id, con=con)
            if current.get("superseded_by") not in {None, replacement_memory_id}:
                raise ValueError("Memory is already superseded by another memory.")
            if current.get("valid_from") and parse_time(current["valid_from"]) >= parse_time(effective):
                raise ValueError("effective_at must be later than the original valid_from.")

            cursor = replacement
            visited = {memory_id}
            while cursor.get("superseded_by"):
                next_id = str(cursor["superseded_by"])
                if next_id in visited:
                    raise ValueError("Supersession would create a cycle.")
                visited.add(next_id)
                cursor = self.get_memory(next_id, con=con)

            now = utc_now()
            con.execute(
                "UPDATE memories SET valid_until=?, superseded_by=?, updated_at=? WHERE id=?",
                (effective, replacement_memory_id, now, memory_id),
            )
            con.execute(
                "UPDATE memories SET valid_from=COALESCE(valid_from, ?), updated_at=? WHERE id=?",
                (effective, now, replacement_memory_id),
            )
            self.index_memory(con, memory_id)
            self.index_memory(con, replacement_memory_id)
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory.superseded",
                    object_type="memory",
                    object_id=memory_id,
                    actor_role="system",
                    source_channel="memory_api",
                    content_origin="derived",
                    derivations=[
                        {
                            "object_type": "memory",
                            "object_id": replacement_memory_id,
                            "relation": "superseded_by",
                        }
                    ],
                    event_time=effective,
                    received_at=now,
                    integrity_tier="durable",
                    payload={"replacement_memory_id": replacement_memory_id},
                )
                self.audit.append_object_event(
                    con,
                    event_type="memory.replacement_activated",
                    object_type="memory",
                    object_id=replacement_memory_id,
                    actor_role="system",
                    source_channel="memory_api",
                    content_origin="derived",
                    derivations=[
                        {
                            "object_type": "memory",
                            "object_id": memory_id,
                            "relation": "supersedes",
                        }
                    ],
                    event_time=effective,
                    received_at=now,
                    integrity_tier="durable",
                    payload={"superseded_memory_id": memory_id},
                )
            return {
                "effective_at": effective,
                "superseded": self.get_memory(memory_id, con=con),
                "replacement": self.get_memory(replacement_memory_id, con=con),
            }

    def forget_legacy_memory(self, legacy_type: str, legacy_id: str, con: sqlite3.Connection) -> None:
        rows = con.execute(
            "SELECT id FROM memories WHERE legacy_memory_type=? AND legacy_memory_id=?",
            (legacy_type, legacy_id),
        ).fetchall()
        for row in rows:
            if self.audit_enabled(con):
                self.audit.append_object_event(
                    con,
                    event_type="memory.forgotten",
                    object_type="memory",
                    object_id=row["id"],
                    actor_role="system",
                    source_channel="legacy_memory_api",
                    content_origin="derived",
                    integrity_tier="durable",
                    payload={"legacy_memory_type": legacy_type, "legacy_memory_id": legacy_id},
                )
            con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (row["id"],))
            con.execute("DELETE FROM memories WHERE id=?", (row["id"],))

    def retrieve(
        self,
        query: str,
        limit: int = 8,
        include_archived: bool = False,
        memory_types: list[str] | None = None,
        update_recall: bool = True,
        temporal_scope: str = "current",
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        types = self.normalize_memory_types(memory_types)
        terms = query_terms(query)
        query_normalized = normalize_text(query)
        results: list[dict[str, Any]] = []
        reference = normalize_timestamp(as_of or utc_now(), field_name="as_of")
        with self.connect() as con:
            fts_hits = self.fts_hits(con, terms)
            clauses = ["memory_type IN ({})".format(",".join("?" for _ in types))]
            params: list[Any] = list(types)
            if not include_archived:
                clauses.append("archived=0")
            self.add_temporal_scope(clauses, params, temporal_scope, reference)
            rows = con.execute(f"SELECT * FROM memories WHERE {' AND '.join(clauses)}", params).fetchall()
            for row in rows:
                memory = self.memory_row(row)
                memory["temporal_status"] = temporal_state(memory, reference)
                keyword = self.keyword_score(query_normalized, terms, memory, fts_hits)
                if keyword <= 0.0:
                    continue
                pinned_bonus = 1.0 if memory["pinned"] or memory["acquisition_mode"] == "user_explicit" else 0.0
                time_score = temporal_relevance(memory, reference)
                relevance = clamp(
                    0.52 * keyword
                    + 0.17 * memory["activation"]
                    + 0.11 * memory["salience"]
                    + 0.09 * memory["stability"]
                    + 0.05 * pinned_bonus
                    + 0.06 * time_score
                )
                components = {
                    "keyword_score": keyword,
                    "activation": memory["activation"],
                    "salience": memory["salience"],
                    "stability": memory["stability"],
                    "retention_score": memory["retention_score"],
                    "pinned_or_explicit_bonus": pinned_bonus,
                    "temporal_relevance": time_score,
                    "temporal_status": memory["temporal_status"],
                    "as_of": reference,
                }
                reasons = ["query matched title, content, or keywords"]
                if memory["activation"] >= 0.75:
                    reasons.append("memory is highly activated")
                if memory["pinned"]:
                    reasons.append("memory is pinned")
                if memory["acquisition_mode"] == "user_explicit":
                    reasons.append("memory was explicitly provided by the user")
                if memory["temporal_status"] != "current":
                    reasons.append(f"memory is {memory['temporal_status']} as of {reference}")
                results.append(
                    {
                        "memory_type": memory["memory_type"],
                        "memory": memory,
                        "relevance": relevance,
                        "reason": "; ".join(reasons),
                        "inject_mode": self.inject_mode(relevance, memory),
                        "components": components,
                    }
                )
            results.sort(key=lambda item: item["relevance"], reverse=True)
            selected = results[: max(1, min(int(limit), 50))]
            if update_recall:
                now = datetime.now(timezone.utc)
                now_text = now.isoformat(timespec="seconds")
                for result in selected:
                    memory = result["memory"]
                    previous = parse_time(memory.get("last_recalled_at"), parse_time(memory["created_at"]))
                    spaced = (now - previous).total_seconds() >= 86400
                    activation = clamp(memory["activation"] + DEFAULT_RECALL_BOOST * (1.0 - memory["activation"]))
                    stability = clamp(memory["stability"] + (DEFAULT_SPACED_STABILITY_BOOST if spaced else 0.0))
                    score = retention_score(activation, memory["salience"], stability, memory["continuity_score"])
                    con.execute(
                        """
                        UPDATE memories
                        SET activation=?, stability=?, retention_score=?, last_recalled_at=?,
                            recall_count=recall_count+1, updated_at=?
                        WHERE id=?
                        """,
                        (activation, stability, score, now_text, now_text, memory["id"]),
                    )
                    con.execute(
                        """
                        INSERT INTO recall_history
                        (id, memory_id, memory_type, query, reason, relevance, inject_mode, created_at)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            new_id("recall"),
                            memory["id"],
                            memory["memory_type"],
                            query,
                            result["reason"],
                            result["relevance"],
                            result["inject_mode"],
                            now_text,
                        ),
                    )
                    if self.audit_enabled(con):
                        self.audit.append_object_event(
                            con,
                            event_type="memory.recalled",
                            object_type="memory",
                            object_id=memory["id"],
                            actor_role="system",
                            source_channel="retrieval",
                            content_origin="derived",
                            derivations=memory.get("derived_from") or [],
                            event_time=now_text,
                            received_at=now_text,
                            integrity_tier="routine",
                            payload={
                                "query_digest": __import__("hashlib").sha256(query.encode("utf-8")).hexdigest(),
                                "relevance": result["relevance"],
                            },
                        )
                    memory["activation"] = activation
                    memory["stability"] = stability
                    memory["retention_score"] = score
                    memory["last_recalled_at"] = now_text
                    memory["recall_count"] += 1
        return selected

    @staticmethod
    def normalize_memory_types(memory_types: list[str] | None) -> list[str]:
        if not memory_types:
            return list(MEMORY_TYPES)
        normalized: list[str] = []
        for memory_type in memory_types:
            canonical = LEGACY_TYPE_MAP.get(memory_type, memory_type)
            if canonical not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory_type: {memory_type}")
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    def keyword_score(
        self,
        query_normalized: str,
        terms: list[str],
        memory: dict[str, Any],
        fts_hits: dict[str, float],
    ) -> float:
        title = normalize_text(memory["title"])
        content = normalize_text(memory["content"])
        keywords = [normalize_text(keyword) for keyword in memory["keywords"]]
        haystack = " ".join((title, content, " ".join(keywords)))
        score = 0.0
        if title and (title in query_normalized or query_normalized in title):
            score += 0.45
        if query_normalized and query_normalized in haystack:
            score += 0.45
        for keyword in keywords:
            if keyword and keyword in query_normalized:
                score += 0.35
        for term in terms:
            if term in title:
                score += 0.18
            elif term in haystack:
                score += 0.08
        score += 0.20 * fts_hits.get(memory["id"], 0.0)
        return clamp(score)

    @staticmethod
    def inject_mode(relevance: float, memory: dict[str, Any]) -> str:
        if relevance >= 0.78:
            return "full" if len(memory["content"]) < 500 else "short"
        if relevance >= 0.45:
            return "short"
        if relevance >= 0.25:
            return "reference_only"
        return "silent"

    def fts_hits(self, con: sqlite3.Connection, terms: list[str]) -> dict[str, float]:
        safe_terms = [term for term in terms if re.fullmatch(r"[a-z0-9_./:-]{2,}", term)]
        if not safe_terms:
            return {}
        expr = " OR ".join(f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in safe_terms[:8])
        rows = con.execute(
            "SELECT memory_id, rank FROM long_term_memory_fts WHERE long_term_memory_fts MATCH ? ORDER BY rank LIMIT 50",
            (expr,),
        ).fetchall()
        return {row["memory_id"]: 1.0 / (1.0 + abs(float(row["rank"]))) for row in rows}

    def index_trace(self, con: sqlite3.Connection, trace_id: str) -> None:
        row = con.execute("SELECT * FROM memory_traces WHERE id=?", (trace_id,)).fetchone()
        con.execute("DELETE FROM memory_trace_fts WHERE trace_id=?", (trace_id,))
        if row is None or row["status"] in {"archived", "consolidated"}:
            return
        con.execute(
            "INSERT INTO memory_trace_fts(trace_id, title, content, keywords) VALUES (?,?,?,?)",
            (trace_id, row["title"], row["content"], " ".join(loads(row["keywords_json"], []))),
        )

    def index_memory(self, con: sqlite3.Connection, memory_id: str) -> None:
        row = con.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory_id,))
        if row is None or row["archived"]:
            return
        con.execute(
            "INSERT INTO long_term_memory_fts(memory_id, memory_type, title, content, keywords) VALUES (?,?,?,?,?)",
            (memory_id, row["memory_type"], row["title"], row["content"], " ".join(loads(row["keywords_json"], []))),
        )

    @staticmethod
    def trace_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "trace_stage": row["trace_stage"],
            "candidate_memory_type": row["candidate_memory_type"],
            "title": row["title"],
            "content": row["content"],
            "keywords": loads(row["keywords_json"], []),
            "acquisition_mode": row["acquisition_mode"],
            "epistemic_status": row["epistemic_status"],
            "epistemic_confidence": row["epistemic_confidence"],
            "activation": row["activation"],
            "salience": row["salience"],
            "stability": row["stability"],
            "continuity_score": row["continuity_score"],
            "retention_score": row["retention_score"],
            "affect_signal": loads(row["affect_signal_json"], {}),
            "capture_score": row["capture_score"],
            "repetition_score": row["repetition_score"],
            "unfinished_score": row["unfinished_score"],
            "confirmation_score": row["confirmation_score"],
            "occurrence_count": row["occurrence_count"],
            "extraction_reasons": loads(row["extraction_reasons_json"], []),
            "content_fingerprint": row["content_fingerprint"],
            "first_observed_at": row["first_observed_at"],
            "last_observed_at": row["last_observed_at"],
            "event_time": row["event_time"],
            "received_at": row["received_at"],
            "persisted_at": row["persisted_at"],
            "source_time": row["source_time"],
            "timezone": row["timezone"],
            "time_source": row["time_source"],
            "ingest_delay_seconds": row["ingest_delay_seconds"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "superseded_by": row["superseded_by"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "source_channel": row["source_channel"],
            "content_origin": row["content_origin"],
            "extractor": row["extractor"],
            "derived_from": loads(row["derived_from_json"], []),
            "latest_audit_event_id": row["latest_audit_event_id"],
            "latest_object_digest": row["latest_object_digest"],
            "temporal_status": temporal_state(dict(row)),
            "evidence_summary": row["evidence_summary"],
            "source_event_ids": loads(row["source_event_ids_json"], []),
            "source": loads(row["source_json"], {}),
            "observation_statement": row["observation_statement"],
            "perspective": row["perspective"],
            "evidence_kind": row["evidence_kind"],
            "observation_fidelity": row["observation_fidelity"],
            "source_reliability": row["source_reliability"],
            "world_hypothesis": row["world_hypothesis"],
            "record_threshold": row["record_threshold"],
            "review_threshold": row["review_threshold"],
            "delete_threshold": row["delete_threshold"],
            "status": row["status"],
            "last_recalled_at": row["last_recalled_at"],
            "recall_count": row["recall_count"],
            "last_decayed_at": row["last_decayed_at"],
            "expires_at": row["expires_at"],
            "consolidated_at": row["consolidated_at"],
            "consolidated_memory_id": row["consolidated_memory_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def memory_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "memory_type": row["memory_type"],
            "title": row["title"],
            "content": row["content"],
            "keywords": loads(row["keywords_json"], []),
            "entities": loads(row["entities_json"], []),
            "acquisition_mode": row["acquisition_mode"],
            "epistemic_status": row["epistemic_status"],
            "epistemic_confidence": row["epistemic_confidence"],
            "activation": row["activation"],
            "salience": row["salience"],
            "stability": row["stability"],
            "continuity_score": row["continuity_score"],
            "retention_score": row["retention_score"],
            "last_recalled_at": row["last_recalled_at"],
            "recall_count": row["recall_count"],
            "last_decayed_at": row["last_decayed_at"],
            "expires_at": row["expires_at"],
            "event_time": row["event_time"],
            "received_at": row["received_at"],
            "persisted_at": row["persisted_at"],
            "source_time": row["source_time"],
            "timezone": row["timezone"],
            "time_source": row["time_source"],
            "ingest_delay_seconds": row["ingest_delay_seconds"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "superseded_by": row["superseded_by"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "source_channel": row["source_channel"],
            "content_origin": row["content_origin"],
            "extractor": row["extractor"],
            "derived_from": loads(row["derived_from_json"], []),
            "latest_audit_event_id": row["latest_audit_event_id"],
            "latest_object_digest": row["latest_object_digest"],
            "temporal_status": temporal_state(dict(row)),
            "pinned": bool(row["pinned"]),
            "archived": bool(row["archived"]),
            "evidence_summary": row["evidence_summary"],
            "source_event_ids": loads(row["source_event_ids_json"], []),
            "source": loads(row["source_json"], {}),
            "metadata": loads(row["metadata_json"], {}),
            "observation_statement": row["observation_statement"],
            "perspective": row["perspective"],
            "evidence_kind": row["evidence_kind"],
            "observation_fidelity": row["observation_fidelity"],
            "source_reliability": row["source_reliability"],
            "world_hypothesis": row["world_hypothesis"],
            "consolidated_at": row["consolidated_at"],
            "legacy_memory_type": row["legacy_memory_type"],
            "legacy_memory_id": row["legacy_memory_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def stats(self) -> dict[str, int]:
        with self.connect() as con:
            return {
                "memory_traces": con.execute("SELECT count(*) FROM memory_traces").fetchone()[0],
                "active_memory_traces": con.execute(
                    "SELECT count(*) FROM memory_traces WHERE status IN ('active','review')"
                ).fetchone()[0],
                "long_term_memories": con.execute("SELECT count(*) FROM memories").fetchone()[0],
                "active_long_term_memories": con.execute("SELECT count(*) FROM memories WHERE archived=0").fetchone()[0],
            }

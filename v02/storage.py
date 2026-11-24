"""Canonical transaction boundary, reusing the signed audit/backup implementation."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from audit_ledger import AuditLedger, OBJECT_TABLES
from backup_restore import BackupRestore
from checkpoint_security import CheckpointSecurity
from .contracts import canonical, digest, now, uid, provenance


class Conflict(ValueError):
    pass


class Missing(KeyError):
    pass


OBJECT_TABLES["v02_record"] = "records"
SCHEMA_VERSION = "5"
KNOWLEDGE_IMMUTABLE_KINDS = frozenset({
    "knowledge_claim_version", "knowledge_source", "knowledge_evidence",
    "knowledge_search", "knowledge_review", "schema_upgrade",
    "knowledge_extraction", "knowledge_migration",
    "knowledge_search_plan", "knowledge_search_attempt", "knowledge_search_observation",
})
EPISODE_IMMUTABLE_KINDS = frozenset({
    "episode_chunk", "episode_summary", "episode", "structured_candidate",
    "structured_review", "artifact_reference", "episode_job",
})


def inspect_schema(path: str | Path):
    """Read the version without creating a database or applying schema statements."""
    path = Path(path).resolve()
    if not path.exists():
        return None
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return None
        if "v02_meta" not in tables:
            raise ValueError("Legacy/unknown database: use explicit offline migration into a new database")
        row = con.execute("SELECT value FROM v02_meta WHERE key='schema_version'").fetchone()
        if row is None:
            raise ValueError("Canonical schema version is missing")
        return row[0]
    finally:
        con.close()


class Storage:
    def __init__(self, path: str | Path, *, allow_pending_migration=False):
        self._configure(path)
        if self.schema_upgrade_marker.exists():
            raise ValueError("Incomplete offline schema upgrade; resume before normal operation")
        version = inspect_schema(self.path)
        if version not in (None, SCHEMA_VERSION):
            raise ValueError("Unsupported canonical schema; explicit offline schema upgrade is required")
        if self.migration_marker.exists() and not allow_pending_migration:
            raise ValueError("Incomplete explicit migration; resume migration before normal operation")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            if version is not None:
                if self.security.read_anchor() is None:
                    raise ValueError("Existing epoch is missing its external anchor; refusing to invent a replacement")
            else:
                con.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                self.security.migrate_schema(con)
                self.security.ensure_identity(con)
                from .observation import install
                install(con)
                con.commit()
        if self.security.read_anchor() is None:
            self.checkpoint("canonical_epoch_created")
        else:
            result = self.verify()
            if not result["valid"]:
                raise ValueError("Canonical integrity check failed; refusing normal operation")

    def _configure(self, path):
        """Configure helpers without reading or writing state; also used offline."""
        self.path = Path(path).resolve()
        self.maintenance_marker = self.path.with_name(self.path.name + ".maintenance")
        self.migration_marker = self.path.with_name(self.path.name + ".migration-pending")
        self.schema_upgrade_marker = self.path.with_name(self.path.name + ".schema-upgrade-pending")
        self.knowledge_migration_marker = self.path.with_name(self.path.name + ".knowledge-migration-pending")
        self.audit = AuditLedger()
        self.security = CheckpointSecurity(self.path, self.audit)
        # Legacy deployment settings must never redirect a new epoch to old keys/anchors.
        self.security.security_dir = self.path.parent / ("." + self.path.stem + "-v02-security")
        self.security.key_dir = self.security.security_dir / "keys"
        self.security.anchor_path = self.security.security_dir / "anchors/latest-checkpoint.json"
        self.security.passphrase = os.getenv("HIPPOCAMPUS_V02_KEY_PASSPHRASE")
        self.backups = BackupRestore(self.path, self.security, self.audit)
        self.backups.backup_dir = self.security.security_dir / "backups"

    @contextmanager
    def connection(self, write=False):
        if self.maintenance_marker.exists():
            raise ValueError("Offline maintenance in progress; normal operations are unavailable")
        if self.schema_upgrade_marker.exists():
            raise ValueError("Offline schema upgrade in progress; normal operations are unavailable")
        if self.knowledge_migration_marker.exists():
            raise ValueError("Offline knowledge migration in progress; resume before normal operation")
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        transaction_id = uuid.uuid4().hex
        con.create_function("observation_tx", 0, lambda: transaction_id)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA secure_delete=ON")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def decode(row):
        return {"id": row["id"], "kind": row["kind"], "principal_id": row["principal_id"],
                "conversation_id": row["conversation_id"], "version": row["version"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "data": json.loads(row["data_json"])}

    def get(self, con, principal, identity, kind=None):
        row = con.execute("SELECT * FROM records WHERE id=? AND principal_id=?", (identity, principal)).fetchone()
        if row is None or (kind and row["kind"] != kind):
            raise Missing("Record not found in authorized scope")
        return self.decode(row)

    def list(self, con, principal, kind, conversation=None):
        sql = "SELECT * FROM records WHERE principal_id=? AND kind=?"
        params = [principal, kind]
        if conversation is not None:
            sql += " AND conversation_id=?"
            params.append(conversation)
        return [self.decode(r) for r in con.execute(sql + " ORDER BY created_at,id", params)]

    def put(self, con, *, identity, principal, kind, conversation, data, reason, expected=None):
        # Offline migration callers may supply their own connection instead of
        # connection(). Bind one transaction identity before triggers compile.
        try:
            con.execute("SELECT observation_tx()")
        except sqlite3.OperationalError:
            transaction_id = uuid.uuid4().hex
            con.create_function("observation_tx", 0, lambda: transaction_id)
        row = con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()
        if row and (row["principal_id"] != principal or row["kind"] != kind or row["conversation_id"] != conversation):
            raise Conflict("Record identity or ownership conflict")
        if (row["version"] if row else None) != expected:
            raise Conflict("Stale record version")
        if row and kind in KNOWLEDGE_IMMUTABLE_KINDS | EPISODE_IMMUTABLE_KINDS:
            raise Conflict("Immutable canonical record; append a new record instead")
        stamp = now()
        if row:
            # Preserve every memory version without copying it into unerasable audit payloads.
            if kind == "memory":
                self.put(con, identity=uid("memory_version", identity, row["version"]), principal=principal,
                         kind="memory_version", conversation=conversation,
                         data={"memory_id": identity, "version": row["version"], "snapshot": json.loads(row["data_json"])},
                         reason="preserve_memory_version")
            elif kind == "knowledge_claim":
                self.put(con, identity=uid("knowledge_claim_version", identity, row["version"]), principal=principal,
                         kind="knowledge_claim_version", conversation=conversation,
                         data={"claim_id": identity, "version": row["version"], "snapshot": json.loads(row["data_json"])},
                         reason="preserve_knowledge_claim_version")
            con.execute("UPDATE records SET data_json=?,version=version+1,updated_at=? WHERE id=?",
                        (canonical(data), stamp, identity))
        else:
            con.execute("INSERT INTO records(id,kind,principal_id,conversation_id,data_json,version,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                        (identity, kind, principal, conversation, canonical(data), stamp, stamp))
        if kind == "memory":
            con.execute("DELETE FROM memory_fts WHERE id=?", (identity,))
            con.execute("INSERT INTO memory_fts(id,content,keywords) VALUES (?,?,?)",
                        (identity, data["content"], " ".join(data.get("keywords", []))))
        elif kind == "knowledge_claim":
            con.execute("DELETE FROM knowledge_fts WHERE id=?", (identity,))
            con.execute("INSERT INTO knowledge_fts(id,content) VALUES (?,?)", (identity, data["proposition"]["text"]))
        elif kind == "event":
            # Historical offline writers retain their original schema contract.
            # Schema 5 builds this derived index explicitly during migration.
            version = con.execute("SELECT value FROM v02_meta WHERE key='schema_version'").fetchone()[0]
            if int(version) >= 5:
                con.execute("INSERT INTO raw_event_fts(id,content) VALUES (?,?)", (identity, data["content"]))
        self.audit.append_object_event(con, event_type="canonical." + kind + (".updated" if row else ".created"),
                                       object_type="v02_record", object_id=identity,
                                       actor_role="system", source_channel="canonical_command",
                                       content_origin="derived", payload={"reason_code": reason})
        return self.get(con, principal, identity)

    def command(self, principal, name, key, payload, operation, *, replay_guard=None):
        if not key or not principal:
            raise ValueError("Principal and idempotency key are required")
        identity = uid("receipt", principal, name, key)
        with self.connection(write=True) as con:
            if any(json.loads(row[0]).get("principal_digest") == digest(principal)
                   for row in con.execute("SELECT data_json FROM records WHERE kind='erasure'")):
                raise Conflict("Erased principal is revoked; explicitly provision a new principal before writing again")
            row = con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()
            if row:
                existing = self.decode(row)["data"]
                if existing["input_digest"] != digest(payload):
                    raise Conflict("Idempotency key reused with different input")
                if replay_guard is not None:
                    replay_guard(con)
                return existing["result"]
            result = operation(con)
            self.put(con, identity=identity, principal=principal, kind="receipt",
                     conversation=payload.get("conversation_id"),
                     data={"command": name, "input_digest": digest(payload), "result": result,
                           "provenance": provenance("domain_command", "command_completed")},
                     reason="command_completed")
            return result

    def verify(self):
        with self.connection() as con:
            audit = self.audit.verify(con)
            security = self.security.verify_checkpoints(con, anchor=self.security.read_anchor())
            # The legacy verifier checks audited objects; also reject unaudited new records.
            untracked = con.execute("SELECT count(*) FROM records WHERE latest_audit_event_id IS NULL").fetchone()[0]
            indexes = self.verify_indexes(con)
            version = con.execute("SELECT value FROM v02_meta WHERE key='schema_version'").fetchone()
            schema_valid = version is not None and version[0] == SCHEMA_VERSION
            return {"valid": audit["valid"] and security["valid"] and not untracked and all(indexes.values()) and schema_valid,
                    "audit": audit, "checkpoints": security, "unaudited_records": untracked,
                    "schema_valid": schema_valid, **indexes}

    @staticmethod
    def verify_indexes(con, *, include_knowledge=True, include_raw=True):
        result = {"fts_valid": False}
        try:
            expected = {(r["id"], json.loads(r["data_json"])["content"], " ".join(json.loads(r["data_json"]).get("keywords", [])))
                        for r in con.execute("SELECT id,data_json FROM records WHERE kind='memory'")}
            actual = [tuple(r) for r in con.execute("SELECT id,content,keywords FROM memory_fts")]
            result["fts_valid"] = len(actual) == len(expected) and set(actual) == expected
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            pass
        if include_knowledge:
            result["knowledge_fts_valid"] = False
            try:
                expected = {(r["id"], json.loads(r["data_json"])["proposition"]["text"])
                            for r in con.execute("SELECT id,data_json FROM records WHERE kind='knowledge_claim'")}
                actual = [tuple(r) for r in con.execute("SELECT id,content FROM knowledge_fts")]
                result["knowledge_fts_valid"] = len(actual) == len(expected) and set(actual) == expected
            except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
                pass
        if include_raw:
            result["raw_event_fts_valid"] = False
            try:
                expected = {(r["id"], json.loads(r["data_json"])["content"])
                            for r in con.execute("SELECT id,data_json FROM records WHERE kind='event'")}
                actual = [tuple(r) for r in con.execute("SELECT id,content FROM raw_event_fts")]
                result["raw_event_fts_valid"] = len(actual) == len(expected) and set(actual) == expected
            except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
                pass
        return result

    def checkpoint(self, reason="manual"):
        with self.connection(write=True) as con:
            current = self.security.read_anchor()
            if current and not self.security.verify_checkpoints(con, anchor=current)["valid"]:
                raise ValueError("Cannot replace invalid checkpoint/anchor")
            checkpoint = self.security.create_checkpoint(con, reason=reason)
            anchor = self.security.anchor_document(con, checkpoint["checkpoint_id"])
        self.security.write_anchor(anchor)
        return {"checkpoint": checkpoint, "anchor": anchor}

    def backup(self, label="canonical"):
        result = self.checkpoint("backup")
        return self.backups.create(checkpoint=result["checkpoint"], anchor=result["anchor"], label=label)

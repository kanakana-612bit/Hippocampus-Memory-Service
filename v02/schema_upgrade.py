"""Explicit offline canonical upgrades; ordinary startup never upgrades a DB."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .contracts import canonical, now, provenance, uid
from .storage import EPISODE_IMMUTABLE_KINDS, SCHEMA_VERSION, Storage, inspect_schema


def _connect(path, *, readonly=False):
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if readonly else sqlite3.connect(path, timeout=1)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _verify(store, con, version):
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")
    audit = store.audit.verify(con)
    checkpoints = store.security.verify_checkpoints(con, anchor=store.security.read_anchor())
    indexes = store.verify_indexes(con, include_knowledge=int(version) >= 2, include_raw=int(version) >= 5)
    unaudited = con.execute("SELECT count(*) FROM records WHERE latest_audit_event_id IS NULL").fetchone()[0]
    if not audit["valid"] or not checkpoints["valid"] or unaudited or not all(indexes.values()):
        raise ValueError("Existing canonical audit, anchor, or index verification failed")
    if version == "1" and con.execute("SELECT 1 FROM records WHERE kind LIKE 'knowledge_%' OR kind='schema_upgrade' LIMIT 1").fetchone():
        raise ValueError("Schema 1 unexpectedly contains knowledge/upgrade records")
    unsupported = []
    if int(version) < 3:
        unsupported.extend(("knowledge_extraction", "knowledge_migration"))
    if int(version) < 4:
        unsupported.extend(("knowledge_search_plan", "knowledge_search_attempt", "knowledge_search_observation", "knowledge_search_lease"))
    if int(version) < 5:
        unsupported.extend((*EPISODE_IMMUTABLE_KINDS, "episode_job_state"))
    if unsupported and con.execute("SELECT 1 FROM records WHERE kind IN (" + ",".join("?" for _ in unsupported) + ") LIMIT 1", unsupported).fetchone():
        raise ValueError("Canonical schema unexpectedly contains records from a newer schema")
    return {"valid": True, "audit_event_count": audit["event_count"],
            "checkpoint_count": checkpoints["checkpoint_count"], **indexes}


def _steps(source, target):
    return [{"from_schema": str(version), "to_schema": str(version + 1)}
            for version in range(int(source), int(target))]


def _apply_step(con, target):
    if target == "2":
        con.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(id UNINDEXED, content)")
    if target == "5":
        # Only an explicit offline upgrade reads primary events for the index.
        # Rebuilding an existing derived table also supports synthetic historical
        # fixtures while preserving every canonical record and audit event.
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS raw_event_fts USING fts5(id UNINDEXED, content)")
        con.execute("DELETE FROM raw_event_fts")
        con.execute("INSERT INTO raw_event_fts(id,content) SELECT id,json_extract(data_json,'$.content') FROM records WHERE kind='event'")
    con.execute("DROP TRIGGER immutable_records")
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    trigger = schema[schema.index("CREATE TRIGGER IF NOT EXISTS immutable_records"):]
    if int(target) < 5:
        trigger = trigger.replace(",\n                 'episode_chunk','episode_summary','episode','structured_candidate','structured_review','artifact_reference','episode_job'", "")
    if target in ("2", "3"):
        trigger = trigger.replace(",\n                 'knowledge_search_plan','knowledge_search_attempt','knowledge_search_observation'", "")
    if target == "2":
        trigger = trigger.replace(",\n                 'knowledge_extraction','knowledge_migration'", "")
    # execute(), unlike executescript(), preserves the surrounding transaction.
    con.execute(trigger)
    con.execute("UPDATE v02_meta SET value=? WHERE key='schema_version'", (target,))


def upgrade(path, operation_id, *, target_schema=SCHEMA_VERSION, apply=False, service_stopped=False):
    """Preserve the audit epoch and resume the same operation after interruption.

    Schema DDL, its immutable receipt, and its signed checkpoint commit together.
    The marker remains until the external anchor is written and reverified. A
    retry can therefore finish anchor publication without replaying schema writes.
    Operation IDs remain globally unique across targets. Explicit targets 2/3/4
    allow historical K1/K2 receipts and interrupted upgrades to retain their meaning;
    they never downgrade a newer database. A 1 -> 5 operation records all DDL steps
    and commits them atomically with one immutable receipt and checkpoint.
    """
    if not operation_id or len(operation_id) > 128:
        raise ValueError("A nonempty operation ID of at most 128 characters is required")
    if target_schema not in ("2", "3", "4", "5"):
        raise ValueError("Target schema must be explicitly supported: 2, 3, 4, or 5")
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("An existing canonical database is required")
    store = Storage.__new__(Storage)
    store._configure(path)
    if store.maintenance_marker.exists() or store.migration_marker.exists() or store.knowledge_migration_marker.exists():
        raise ValueError("Finish the existing maintenance/migration before a schema upgrade")
    marker = store.schema_upgrade_marker
    pending = marker.exists()
    if pending:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        if recorded.get("operation_id") != operation_id or recorded.get("database") != str(path):
            raise ValueError("Pending schema upgrade belongs to a different operation")
        if recorded.get("to_schema") != target_schema:
            raise ValueError("Pending schema upgrade has a different target; resume its recorded target first")
    version = inspect_schema(path)
    if version not in ("1", "2", "3", "4", "5"):
        raise ValueError("Only canonical schemas 1, 2, 3, 4, and 5 are supported")
    receipt_id = uid("schema_upgrade", operation_id)
    con = _connect(path, readonly=True)
    try:
        verification = _verify(store, con, version)
        receipt = con.execute("SELECT * FROM records WHERE id=? AND kind='schema_upgrade'", (receipt_id,)).fetchone()
        receipt_data = json.loads(receipt["data_json"]) if receipt is not None else None
        if receipt_data is not None:
            if receipt_data.get("operation_id") != operation_id or receipt_data.get("to_schema") != target_schema:
                raise ValueError("Operation ID already belongs to a different schema upgrade target")
            if int(version) < int(target_schema):
                raise ValueError("Schema version precedes its applied upgrade receipt")
            if pending and version != target_schema:
                raise ValueError("Pending operation does not match the current committed schema; refusing anchor rollback")
        elif int(version) >= int(target_schema):
            raise ValueError("Requested target was not applied by this operation ID; downgrades are not supported")
    finally:
        con.close()
    source_version = receipt_data["from_schema"] if receipt_data else version
    if pending and recorded.get("from_schema") != source_version:
        raise ValueError("Pending marker source schema does not match its operation")
    plan = {"operation_id": operation_id, "from_schema": source_version, "to_schema": target_schema,
            "steps": _steps(source_version, target_schema),
            "current_schema": version, "applied": False, "pending": pending,
            "preserve_audit_epoch": True, "verification": verification}
    if receipt_data is not None and not pending:
        return {**plan, "applied": True, "already_applied": True}
    if not apply:
        return plan
    if not service_stopped:
        raise ValueError("Schema upgrade requires explicit confirmation that the service is stopped")
    if not pending:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(canonical({"operation_id": operation_id, "database": str(path),
                                    "from_schema": source_version, "to_schema": target_schema, "created_at": now()}))
            handle.flush()
            os.fsync(handle.fileno())
    con = _connect(path)
    try:
        con.execute("BEGIN EXCLUSIVE")
        current = con.execute("SELECT value FROM v02_meta WHERE key='schema_version'").fetchone()[0]
        if current != version:
            raise ValueError("Schema changed after planning; resume the recorded operation")
        _verify(store, con, version)
        checkpoint_reason = "canonical_schema_upgrade:" + operation_id
        if receipt_data is None:
            for step in plan["steps"]:
                _apply_step(con, step["to_schema"])
            # Reject invalid primary-text/index mappings inside the same
            # transaction, before publishing an upgraded receipt or anchor.
            _verify(store, con, target_schema)
            store.put(con, identity=receipt_id, principal="$offline-maintenance", kind="schema_upgrade", conversation=None,
                      data={"operation_id": operation_id, "from_schema": source_version, "to_schema": target_schema,
                            "steps": plan["steps"],
                            "provenance": provenance("explicit_offline_operator", "canonical_schema_upgrade")},
                      reason="canonical_schema_upgrade")
            checkpoint = store.security.create_checkpoint(con, reason=checkpoint_reason)
            checkpoint_id = checkpoint["checkpoint_id"]
        else:
            checkpoint = con.execute("SELECT checkpoint_id FROM audit_checkpoints WHERE json_extract(payload_json,'$.reason')=?",
                                     (checkpoint_reason,)).fetchone()
            if checkpoint is None:
                raise ValueError("Committed schema upgrade checkpoint is missing")
            checkpoint_id = checkpoint["checkpoint_id"]
        anchor = store.security.anchor_document(con, checkpoint_id)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    store.security.write_anchor(anchor)
    con = _connect(path, readonly=True)
    try:
        verification = _verify(store, con, target_schema)
        if store.security.read_anchor()["checkpoint"]["checkpoint_id"] != checkpoint_id:
            raise ValueError("Schema upgrade anchor publication could not be verified")
    finally:
        con.close()
    marker.unlink()
    return {**plan, "current_schema": target_schema, "applied": True, "pending": False,
            "already_applied": receipt_data is not None, "checkpoint_id": checkpoint_id,
            "verification": verification}

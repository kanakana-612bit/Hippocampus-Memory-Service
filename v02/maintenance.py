"""Offline, explicitly requested administration; never reachable from model-facing APIs."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from .contracts import canonical, digest, now, provenance, uid
from .storage import SCHEMA_VERSION, Storage, inspect_schema


def existing_store(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("An existing canonical database is required")
    return Storage(path)


def restore(path, filename, *, apply=False, service_stopped=False):
    store = existing_store(path)
    candidate = store.backups._backup_path(filename)
    if not candidate.is_file() or inspect_schema(candidate) != SCHEMA_VERSION:
        raise ValueError("Backup schema must match the current canonical schema before restore")
    backup_con = sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)
    backup_con.row_factory = sqlite3.Row
    try:
        if not all(store.verify_indexes(backup_con).values()):
            raise ValueError("Backup canonical indexes failed integrity verification")
    finally:
        backup_con.close()
    with store.connection() as con:
        plan = store.backups.plan_restore(filename, con)
    if not apply:
        return plan
    if not service_stopped or not plan["valid"]:
        raise ValueError("A valid restore plan and stopped service are required")
    # Pin all outstanding writes before deciding whether rollback needs a new branch.
    store.checkpoint("pre_restore")
    previous = store.security.read_anchor()
    store.maintenance_marker.write_text(canonical({"operation": "restore", "filename": filename}), encoding="utf-8")
    con = sqlite3.connect(store.path, timeout=1)
    try:
        con.execute("BEGIN EXCLUSIVE")
        con.rollback()
    finally:
        con.close()
    # On failure the maintenance marker deliberately remains: no partially restored runtime.
    result = store.backups.restore_offline(filename, previous_anchor=previous)
    con = sqlite3.connect(store.path)
    con.row_factory = sqlite3.Row
    try:
        branch = store.security.adopt_restore_branch(con, previous)
        anchor = store.security.anchor_document(con, branch["checkpoint"]["checkpoint_id"])
        con.commit()
    finally:
        con.close()
    store.security.write_anchor(anchor)
    store.maintenance_marker.unlink()
    verification = existing_store(path).verify()
    return {**result, "branch": branch["branch_id"], "verification": verification}


def _inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Maintenance target escapes its named directory")
    return path


def erase_principal(path, principal, operation_id, *, apply=False, service_stopped=False,
                    confirm_principal=None, purge_managed_backups=False):
    """Create a clean audit epoch, retaining other principals' exact record versions.

    Scope is one whole principal (not one node). Known external copies are reported,
    never silently removed. Purging all managed backups is a separate explicit choice
    because a backup may include multiple principals.
    """
    if not principal or not operation_id:
        raise ValueError("Principal and explicit operation ID are required")
    store = existing_store(path)
    receipt_id = uid("erasure", operation_id)
    with store.connection() as con:
        prior = con.execute("SELECT data_json FROM records WHERE id=? AND kind='erasure'", (receipt_id,)).fetchone()
        if prior:
            result = json.loads(prior[0])
            if result["principal_digest"] != digest(principal):
                raise ValueError("Erasure operation ID belongs to a different scope")
            return {**result, "already_applied": True}
        rows = [dict(r) for r in con.execute("SELECT * FROM records ORDER BY created_at,id")]
    victims = [r for r in rows if r["principal_id"] == principal]
    if not victims:
        raise ValueError("No records in the requested scope")
    external = sorted({json.loads(r["data_json"])["source_path"] for r in victims if r["kind"] == "migration"})
    external += [str(p) for p in store.path.parent.glob(store.path.stem + "-pre-restore-*.db")]
    backup_roots = [store.backups.backup_dir] + [p / "backups" for p in store.path.parent.glob(store.security.security_dir.name + "-retired-*")]
    backups = []
    for root in backup_roots:
        _inside(root, store.path.parent)
        for entry in root.glob("*"):
            target = _inside(entry, root)
            if not target.is_file():
                raise ValueError("Unrecognized managed backup entry; inspect it before erasure")
            backups.append(str(target))
    backups.sort()
    plan = {"operation_id": operation_id, "principal_digest": digest(principal),
            "scope": "entire_principal", "record_count": len(victims),
            "retained_other_records": len(rows) - len(victims), "managed_backup_files": backups,
            "known_external_copies": external, "purge_managed_backups": purge_managed_backups,
            "new_audit_epoch": True, "applied": False, "complete": False}
    if not apply:
        return plan
    if not service_stopped or confirm_principal != principal:
        raise ValueError("Offline erasure requires explicit matching principal confirmation")
    # A new signed epoch removes old identifiers, audit digests, runs, receipts and FTS
    # containing the erased principal. No original database recovery copy is produced.
    stage = _inside(store.path.parent / (".v02-erase-" + uuid.uuid4().hex), store.path.parent)
    stage.mkdir()
    staged = Storage(stage / store.path.name)
    store.maintenance_marker.write_text(canonical({"operation": "erase", "operation_id": operation_id,
                                                   "stage": str(stage)}), encoding="utf-8")
    con = sqlite3.connect(store.path, timeout=1)
    try:
        con.execute("BEGIN EXCLUSIVE")
        # Block a stale plan if a writer completed between planning and maintenance.
        con.row_factory = sqlite3.Row
        latest = [dict(r) for r in con.execute("SELECT * FROM records ORDER BY created_at,id")]
        if latest != rows:
            raise ValueError("Store changed since erasure planning; no records were removed")
        con.rollback()
    finally:
        con.close()
    retired = _inside(store.path.parent / (store.security.security_dir.name + "-retired-" + digest(operation_id)[:12]), store.path.parent)
    if retired.exists():
        raise ValueError("Retired epoch directory already exists")
    effective_backups = [retired / "backups" / Path(p).name if Path(p).parent == store.backups.backup_dir.resolve() else Path(p) for p in backups]
    remaining_backups = [] if purge_managed_backups else [str(p) for p in effective_backups]
    result = {**plan, "applied": True, "known_external_copies": external,
              "managed_backup_files": [] if purge_managed_backups else remaining_backups,
              "purged_backup_file_count": len(backups) if purge_managed_backups else 0,
              "remaining_managed_copies": remaining_backups,
              "complete": not external and not remaining_backups,
              "completion_scope": "application-managed logical copies; external media and frontend/provider copies not attested",
              "created_at": now(), "provenance": provenance("explicit_offline_operator", "principal_erasure_new_epoch")}
    with staged.connection(write=True) as target:
        columns = "id,kind,principal_id,conversation_id,data_json,version,created_at,updated_at"
        for row in rows:
            if row["principal_id"] == principal:
                continue
            target.execute("INSERT INTO records(" + columns + ") VALUES (?,?,?,?,?,?,?,?)",
                           tuple(row[k] for k in columns.split(",")))
            if row["kind"] == "memory":
                data = json.loads(row["data_json"])
                target.execute("INSERT INTO memory_fts(id,content,keywords) VALUES (?,?,?)",
                               (row["id"], data["content"], " ".join(data.get("keywords", []))))
            elif row["kind"] == "knowledge_claim":
                data = json.loads(row["data_json"])
                target.execute("INSERT INTO knowledge_fts(id,content) VALUES (?,?)", (row["id"], data["proposition"]["text"]))
            elif row["kind"] == "event":
                data = json.loads(row["data_json"])
                target.execute("INSERT INTO raw_event_fts(id,content) VALUES (?,?)", (row["id"], data["content"]))
            staged.audit.append_object_event(target, event_type="canonical.epoch.retained", object_type="v02_record",
                object_id=row["id"], source_channel="explicit_erasure", payload={"reason_code": "other_principal_preserved"})
        staged.put(target, identity=receipt_id, principal="$offline-maintenance", kind="erasure", conversation=None,
                   data=result, reason="explicit_erasure")
    staged.checkpoint("erasure_epoch_ready")
    if not staged.verify()["valid"]:
        raise RuntimeError("New epoch failed verification; original remains behind maintenance marker")
    store.security.security_dir.rename(retired)
    try:
        staged.security.security_dir.rename(store.security.security_dir)
        os.replace(staged.path, store.path)
    except BaseException:
        # Remain offline; paths in the marker and deterministic retired folder allow recovery.
        raise
    if purge_managed_backups:
        for filename in effective_backups:
            target = _inside(filename, store.path.parent)
            target.unlink()
    # Keys/anchors are non-content but retired copies remain named for inspection.
    stage.rmdir()  # All generated files have been moved, no recursive removal.
    store.maintenance_marker.unlink()
    verification = existing_store(path).verify()
    return {**result, "verification": verification}

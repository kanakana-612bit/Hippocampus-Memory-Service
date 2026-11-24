"""Explicit, one-way, read-only-source migration. Never imported by the web runtime."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from audit_ledger import AuditLedger
from temporal_memory import normalize_timestamp
from .contracts import MemoryInput, canonical, digest, now, provenance, uid
from .service import Service


def read_source(path):
    path = Path(path).resolve(strict=True)
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "v02_meta" in tables or "raw_messages" not in tables:
            raise ValueError("Source must be a legacy Hippocampus database")
        names = ["raw_messages", "memories", "memory_traces", "episodic_memories", "project_memories",
                 "persistent_memories", "daily_summaries", "dialogue_events", "dialogue_path_events",
                 "memory_evidence_links", "memory_links", "provenance_edges", "audit_events", "schema_migrations"]
        data = {name: [dict(row) for row in con.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                for name in names if name in tables}
        verification = AuditLedger().verify(con) if "audit_events" in tables else None
        if verification and not verification["valid"]:
            raise ValueError("Source audit verification failed")
        try:
            canonical_ids = {r[0] for r in con.execute("SELECT id FROM canonical_dialogue_events")}
        except sqlite3.OperationalError:
            canonical_ids = set()  # Do not invent a selected branch for pre-canonical data.
        return data, canonical_ids, verification
    finally:
        con.close()


def migrate(source, destination, principal, *, apply=False):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if source == destination or not principal:
        raise ValueError("A different destination and explicit owner are required")
    data, canonical_ids, verification = read_source(source)
    source_digest = digest(data)
    manifest_id = uid("migration", source_digest, principal, "v0.2")
    report = {"migration_id": manifest_id, "source_digest": source_digest,
              "counts": {k: len(v) for k, v in data.items()},
              "source_audit_valid": verification["valid"] if verification else None,
              "source_head": verification["head_event_hash"] if verification else None,
              "external_source_anchor_verified": False, "applied": False,
              "policy": "canonical_preferred; legacy_confidence_unassessed; no_implicit_episode_identity"}
    if not apply:
        return report
    marker = destination.with_name(destination.name + ".migration-pending")
    if marker.exists() and marker.read_text(encoding="utf-8") != manifest_id:
        raise ValueError("Pending migration has a different source or owner")
    service = Service(destination, _migration=True)
    with service.db.connection() as con:
        manifests = service.db.list(con, principal, "migration")
        if manifests:
            if len(manifests) == 1 and manifests[0]["id"] == manifest_id:
                if marker.exists():
                    service.db.checkpoint("migration_resumed")
                    marker.unlink()
                return {**manifests[0]["data"]["report"], "already_applied": True}
            raise ValueError("Destination already has a different migration")
        if con.execute("SELECT 1 FROM records LIMIT 1").fetchone():
            raise ValueError("Migration destination must be empty")
    marker.write_text(manifest_id, encoding="utf-8")
    sidecars = {r["id"]: r for r in data.get("dialogue_events", [])}
    raw = {r["id"]: r for r in data["raw_messages"]}
    canonical_rows = data.get("memories", [])
    represented = {(r.get("legacy_memory_type"), r.get("legacy_memory_id")) for r in canonical_rows}
    rows = [("memories", r) for r in canonical_rows] + [("memory_traces", r) for r in data.get("memory_traces", [])]
    for table, kind in [("episodic_memories", "episodic"), ("project_memories", "project"), ("persistent_memories", "persistent")]:
        rows.extend((table, r) for r in data.get(table, []) if (kind, r["id"]) not in represented)
    used = set(raw)
    mapping = {}
    for table, row in rows:
        identity = row["id"] if row["id"] not in used else uid("imported", table, row["id"])
        used.add(identity)
        mapping[(table, row["id"])] = identity
    imported, quarantined = [], []
    with service.db.connection(write=True) as con:
        for identity, row in raw.items():
            if identity.startswith("candidate_audit:"):
                # Preserve legacy material as quarantined archive, never a dialogue event.
                kind = "legacy_archive"
                event = {"legacy_table": "raw_messages", "snapshot": row, "quarantined": True}
            else:
                sidecar = sidecars.get(identity, {})
                event = {"id": identity, "role": row["role"], "content": row["content"],
                         "actor_id": row.get("actor_id"), "logical_actor_id": sidecar.get("logical_actor_id"),
                         "model_provenance": json.loads(sidecar.get("model_provenance_json") or "{}"),
                         "source_message_id": sidecar.get("source_message_id"), "sandbox_id": None,
                         "event_time": normalize_timestamp(row.get("event_time") or row["created_at"], row.get("timezone") or "UTC"),
                         "migration_original": row, "provenance": provenance("explicit_migration", "source_preserved")}
                kind = "event"
            service.db.put(con, identity=identity, principal=principal, kind=kind, conversation=row["conversation_id"],
                           data=event, reason="source_migrated")
        conversations = sorted({r["conversation_id"] for r in raw.values()})
        for conversation in conversations:
            paths = [p for p in data.get("dialogue_path_events", []) if p["conversation_id"] == conversation]
            if paths:
                latest = max(paths, key=lambda p: p["sequence"])
                ids = json.loads(latest["event_ids_json"])
            else:
                events = [r for r in raw.values() if r["conversation_id"] == conversation and r["id"] in canonical_ids]
                events.sort(key=lambda r: (r.get("event_sequence") or 0, r["created_at"], r["id"]))
                ids = [r["id"] for r in events]
            ids = [ref for ref in ids if ref in canonical_ids and not ref.startswith("candidate_audit:")]
            revision = uid("imported_path", manifest_id, conversation)
            path_data = {"revision_key": revision, "event_ids": ids, "previous_revision": None,
                         "provenance": provenance("explicit_migration", "source_canonical_path")}
            for kind, identity in [("revision", uid("revision", principal, conversation, revision)),
                                   ("path", uid("path", principal, conversation))]:
                service.db.put(con, identity=identity, principal=principal, kind=kind, conversation=conversation,
                               data=path_data, reason="path_migrated")
        for table, row in rows:
            identity = mapping[(table, row["id"])]
            source_info = json.loads(row.get("source_json") or "{}")
            refs = json.loads(row.get("source_event_ids_json") or "[]")
            if not refs:
                refs = list(source_info.get("source_event_ids") or source_info.get("message_ids") or [])
                ref = source_info.get("message_id") or source_info.get("source_message_id")
                if ref:
                    refs.append(ref)
            derivations = json.loads(row.get("derived_from_json") or "[]")
            refs = sorted(set(refs) | {d["object_id"] for d in derivations if isinstance(d, dict) and d.get("object_type") == "raw_message"})
            parent_refs = [mapping.get(("memories" if d["object_type"] == "memory" else "memory_traces", d["object_id"]), d["object_id"])
                           for d in derivations if isinstance(d, dict) and d.get("object_type") in {"memory", "memory_trace"}]
            owners = {raw[r]["conversation_id"] for r in refs if r in raw}
            conversation = row.get("conversation_id") or source_info.get("conversation_id") or (next(iter(owners)) if len(owners) == 1 else "migration:unscoped")
            is_quarantined = (conversation == "migration:unscoped" or len(owners) > 1 or
                              (not refs and not parent_refs) or
                              any(ref not in canonical_ids for ref in refs) or
                              bool(row.get("archived")) or row.get("status") in {"archived", "consolidated"})
            memory_type = row.get("memory_type") or row.get("candidate_memory_type") or {
                "episodic_memories": "episodic", "project_memories": "prospective", "persistent_memories": "semantic"}.get(table, "episodic")
            item = MemoryInput(conversation_id=conversation, id=identity,
                content=row.get("content") or row.get("summary") or "[empty legacy record]", title=row.get("title") or "",
                memory_type=memory_type, node_role="legacy_unknown", origin="migration",
                keywords=json.loads(row.get("keywords_json") or "[]"), source_event_ids=refs, source_memory_ids=parent_refs,
                retrieval_weight=row.get("activation", row.get("recency_score", row.get("importance_score", .5))),
                salience=row.get("salience", row.get("importance_score", .5)), stability=row.get("stability", .1),
                epistemic_status=row.get("epistemic_status") or "inferred",
                assessment={"status": "legacy_unassessed"},
                event_time=row.get("event_time"), valid_from=row.get("valid_from"), valid_until=row.get("valid_until"))
            item_data = item.model_dump()
            item_data.update(generalization_weight=0.0, generalization_status="unevaluated", quarantined=is_quarantined,
                             recall_count=row.get("recall_count", 0), stage="candidate" if table == "memory_traces" else "retained",
                             migration_original={"table": table, "row": row},
                             provenance=provenance("explicit_migration", "canonical_preferred_no_confidence_inference"))
            service.db.put(con, identity=identity, principal=principal, kind="memory", conversation=conversation,
                           data=item_data, reason="memory_migrated")
            imported.append(identity)
            if is_quarantined:
                quarantined.append(identity)
        for table in ("daily_summaries", "dialogue_path_events", "memory_evidence_links", "memory_links"):
            for row in data.get(table, []):
                service.db.put(con, identity=uid("legacy_archive", table, row.get("id")), principal=principal,
                               kind="legacy_archive", conversation=row.get("conversation_id"),
                               data={"table": table, "snapshot": row}, reason="legacy_history_preserved")
        report.update(applied=True, imported_memory_count=len(imported), quarantined_count=len(quarantined),
                      uid_mapping=[{"table": t, "source_id": old, "canonical_id": new} for (t, old), new in mapping.items()])
        service.db.put(con, identity=manifest_id, principal=principal, kind="migration", conversation=None,
                       data={"report": report, "source_path": str(source), "source_deleted": False,
                             "provenance": provenance("offline_migration", "new_schema_epoch")}, reason="migration_completed")
    service.db.checkpoint("migration_completed")
    if not service.db.verify()["valid"]:
        raise RuntimeError("Migrated database failed verification")
    marker.unlink()
    return report

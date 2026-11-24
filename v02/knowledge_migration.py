"""Explicit one-way semantic-memory candidate import; never loaded by the API.

Planning reads a canonical database without modifying it. Apply consumes that
saved manifest, uses the ordinary extraction transaction implementation, and
never treats the supplied proposition or an old confidence as established fact.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from .contracts import Contract, canonical, digest, now, provenance, timestamp, uid
from .extraction_contracts import ExtractionConfidence, ExtractionSubmit
from .knowledge_commands import InputScope, SourceCreate
from .knowledge_contracts import Proposition, RecordRef, Text
from .schema_upgrade import _verify
from .storage import SCHEMA_VERSION, Conflict, Missing, Storage, inspect_schema


FORMAT = "knowledge-candidate-migration.k2.v1"
HOST = "offline:knowledge-candidate-migration"
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class EventSpan(Contract):
    event_ref: RecordRef
    event_digest: Digest
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=1)
    quote: Text
    parent_source_refs: tuple[RecordRef, ...] = ()

    @model_validator(mode="after")
    def nonempty(self):
        if self.start >= self.end:
            raise ValueError("A nonempty Unicode-codepoint span is required")
        return self


class MigrationItem(Contract):
    memory_ref: RecordRef
    memory_digest: Digest
    scope: InputScope
    path_revision: Text
    proposition: Proposition
    spans: tuple[EventSpan, ...] = Field(min_length=1)
    confidence: ExtractionConfidence = Field(default_factory=ExtractionConfidence)
    reason: Text
    limitations: tuple[Text, ...] = ()


class MigrationInput(Contract):
    items: tuple[MigrationItem, ...] = Field(min_length=1)


def _connect(path, *, readonly=False):
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if readonly else sqlite3.connect(path, timeout=1)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    if readonly:
        con.execute("PRAGMA query_only=ON")
    return con


def _store(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or inspect_schema(path) != SCHEMA_VERSION:
        raise ValueError("An existing database at the current canonical schema is required")
    store = Storage.__new__(Storage)
    store._configure(path)
    if any(marker.exists() for marker in (store.maintenance_marker, store.migration_marker, store.schema_upgrade_marker)):
        raise ValueError("Finish other maintenance before candidate migration")
    return store


def _service(store):
    # Skip runtime startup while the offline marker deliberately blocks it.
    from .api import Identity
    from .extraction_service import ExtractionService
    from .knowledge_service import KnowledgeService
    from .service import Service
    service = Service.__new__(Service)
    service.db = store
    service.knowledge = KnowledgeService(service)
    service.extraction = ExtractionService(service)
    return service, Identity


def _baseline(con):
    records = [dict(row) for row in con.execute("SELECT * FROM records ORDER BY id")]
    audit = [dict(row) for row in con.execute("SELECT * FROM audit_events ORDER BY sequence")]
    memory_fts = [tuple(row) for row in con.execute("SELECT id,content,keywords FROM memory_fts ORDER BY id")]
    return {"schema": con.execute("SELECT value FROM v02_meta WHERE key='schema_version'").fetchone()[0],
            "records": [{"id": row["id"], "digest": digest(row)} for row in records],
            "record_count": len(records), "records_digest": digest(records),
            "audit_event_count": len(audit), "audit_prefix_digest": digest(audit),
            "memory_fts_digest": digest(memory_fts)}


def _preserved(con, baseline):
    for expected in baseline["records"]:
        row = con.execute("SELECT * FROM records WHERE id=?", (expected["id"],)).fetchone()
        if row is None or digest(dict(row)) != expected["digest"]:
            raise Conflict("Candidate migration modified an existing canonical record")
    audit = [dict(row) for row in con.execute("SELECT * FROM audit_events ORDER BY sequence LIMIT ?",
                                             (baseline["audit_event_count"],))]
    fts = [tuple(row) for row in con.execute("SELECT id,content,keywords FROM memory_fts ORDER BY id")]
    if digest(audit) != baseline["audit_prefix_digest"] or digest(fts) != baseline["memory_fts_digest"]:
        raise Conflict("Candidate migration modified an existing audit prefix or memory index")


class Quarantine(ValueError):
    pass


def _lineage(con, store, principal, memory, scope, active, seen=None):
    seen = set(seen or ())
    if memory["id"] in seen:
        raise Quarantine("cyclic_memory_lineage")
    seen.add(memory["id"])
    data = memory["data"]
    if memory["conversation_id"] != scope.conversation_id:
        raise Quarantine("lineage_conversation_mismatch")
    if data.get("sandbox_id") != scope.sandbox_id:
        raise Quarantine("lineage_fiction_mismatch")
    if data.get("quarantined"):
        raise Quarantine("memory_already_quarantined")
    event_ids = set(data.get("source_event_ids", []))
    records = [memory]
    try:
        for parent in data.get("source_memory_ids", []):
            origins, ancestors = _lineage(con, store, principal,
                store.get(con, principal, parent, "memory"), scope, active, seen)
            event_ids.update(origins)
            records.extend(ancestors)
        if not event_ids:
            raise Quarantine("original_events_untraceable")
        for identity in event_ids:
            event = store.get(con, principal, identity, "event")
            if event["conversation_id"] != scope.conversation_id:
                raise Quarantine("lineage_conversation_mismatch")
            if event["data"].get("sandbox_id") != scope.sandbox_id:
                raise Quarantine("lineage_fiction_mismatch")
            if identity not in active:
                raise Quarantine("lineage_event_off_path")
            records.append(event)
    except Missing as exc:
        raise Quarantine("lineage_reference_missing") from exc
    return event_ids, records


def _entry(con, service, identity, operation_id, number, item):
    scope = service.knowledge._scope(identity, item.scope)
    try:
        memory = service.db.get(con, identity.principal, item.memory_ref.id, "memory")
        if memory["version"] != item.memory_ref.version:
            raise Quarantine("stale_memory_version")
        if digest(memory["data"]) != item.memory_digest:
            raise Quarantine("memory_digest_mismatch")
        if memory["data"].get("memory_type") != "semantic":
            raise Quarantine("memory_not_semantic")
        path = service.knowledge._path(con, scope, item.path_revision)
        events, ancestors = _lineage(con, service.db, identity.principal, memory, scope,
                                     set(path["data"]["event_ids"]))
        sources, spans = {}, []
        for span in item.spans:
            if span.event_ref.id not in events:
                raise Quarantine("span_event_not_in_memory_lineage")
            event = service.knowledge._resolve(con, identity.principal, span.event_ref, "event")
            content = event["data"]["content"]
            if digest(content) != span.event_digest:
                raise Quarantine("event_digest_mismatch")
            if span.end > len(content) or content[span.start:span.end] != span.quote:
                raise Quarantine("span_quote_mismatch")
            # Re-registering a raw event must not erase lineage already known
            # through a canonical source for that same event and exact scope.
            known_parents = set()
            for existing in service.db.list(con, identity.principal, "knowledge_source", scope.conversation_id):
                data = existing["data"]
                if data["scope"] != scope.model_dump() or data["event_ref"] != span.event_ref.model_dump():
                    continue
                parents = frozenset((ref["id"], ref["version"]) for ref in data["parent_source_refs"])
                if parents:
                    known_parents.add(parents)
            if len(known_parents) > 1:
                raise Quarantine("conflicting_known_source_lineage")
            supplied = frozenset((ref.id, ref.version) for ref in span.parent_source_refs)
            if known_parents and supplied != next(iter(known_parents)):
                raise Quarantine("known_source_lineage_omitted_or_changed")
            for parent in span.parent_source_refs:
                resolved = service.knowledge._source(con, scope, parent, path)
                if any(ref["id"] not in events for ref in resolved["data"]["event_refs"]):
                    raise Quarantine("parent_source_outside_memory_lineage")
            key = uid("migration_source", operation_id, scope.model_dump(), span.event_ref.model_dump(),
                      [ref.model_dump() for ref in span.parent_source_refs])
            request = SourceCreate(idempotency_key=key, reason=item.reason, scope=item.scope,
                path_revision=item.path_revision, event_ref=span.event_ref, parent_source_refs=span.parent_source_refs)
            source_id = uid("knowledge_source", identity.principal, key)
            sources[source_id] = request.model_dump()
            spans.append({"source_ref": {"id": source_id, "version": 1}, "start": span.start,
                          "end": span.end, "quote": span.quote, "unit": "unicode_codepoint"})
        request = ExtractionSubmit(idempotency_key=uid("migration_extract", operation_id, number),
            reason=item.reason, path_revision=item.path_revision, scope=item.scope,
            extractor_version="explicit_proposal.v1", proposals=[{"proposition": item.proposition.model_dump(),
                "spans": spans, "confidence": item.confidence.model_dump(), "reason": item.reason,
                "limitations": list(item.limitations)}])
        fingerprint = request.proposals[0].processing_fingerprint(scope)
        claim_ref = {"id": uid("knowledge_claim", "extraction", identity.principal, fingerprint), "version": 1}
        extraction_ref = {"id": uid("knowledge_extraction", identity.principal, fingerprint), "version": 1}
        pinned = {row["id"]: {"id": row["id"], "version": row["version"], "digest": digest(row["data"])}
                  for row in ancestors}
        return {"item": number, "memory_ref": item.memory_ref.model_dump(), "status": "ready",
                "source_requests": sources, "extraction_request": request.model_dump(),
                "planned_claim_ref": claim_ref, "planned_extraction_ref": extraction_ref,
                "lineage": [pinned[key] for key in sorted(pinned)]}
    except (Quarantine, Missing, Conflict, ValueError) as exc:
        return {"item": number, "memory_ref": item.memory_ref.model_dump(), "status": "quarantined",
                "reason": str(exc) if not isinstance(exc, Missing) else "reference_missing_in_authorized_scope"}


def plan(path, principal, operation_id, value):
    """Return a read-only plan to save; content/digests remain local sensitive data."""
    if not principal or not operation_id or len(operation_id) > 128:
        raise ValueError("An owner and operation ID of at most 128 characters are required")
    inputs = MigrationInput.model_validate(value)
    store = _store(path)
    if store.knowledge_migration_marker.exists():
        raise ValueError("Resume the saved manifest; do not replace a pending migration plan")
    service, Identity = _service(store)
    identity = Identity(principal, HOST, frozenset({"write"}))
    con = _connect(store.path, readonly=True)
    try:
        con.execute("BEGIN")
        _verify(store, con, SCHEMA_VERSION)
        baseline = _baseline(con)
        entries = [_entry(con, service, identity, operation_id, index, item) for index, item in enumerate(inputs.items)]
    finally:
        con.close()
    manifest = {"format": FORMAT, "operation_id": operation_id, "principal_id": principal,
                "actor_host_id": HOST, "database": str(store.path), "created_at": now(),
                "input": inputs.model_dump(), "baseline": baseline, "entries": entries,
                "ready_count": sum(item["status"] == "ready" for item in entries),
                "quarantined_count": sum(item["status"] == "quarantined" for item in entries),
                "applied": False, "policy": "explicit_proposals_only; provisional; preserve_existing_records"}
    return {**manifest, "manifest_digest": digest(manifest)}


def save_manifest(manifest, filename):
    """Never silently overwrite an operator's fixed dry-run plan."""
    with Path(filename).open("x", encoding="utf-8") as handle:
        handle.write(canonical(manifest))
        handle.flush()
        os.fsync(handle.fileno())


def _load_manifest(filename, path, operation_id):
    value = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
    expected = value.get("manifest_digest")
    body = {key: item for key, item in value.items() if key != "manifest_digest"}
    if expected != digest(body) or value.get("format") != FORMAT or value.get("applied") is not False:
        raise ValueError("Invalid or modified saved candidate migration manifest")
    if value.get("operation_id") != operation_id or value.get("database") != str(path):
        raise ValueError("Saved manifest belongs to a different operation or database")
    if value.get("actor_host_id") != HOST or not value.get("principal_id"):
        raise ValueError("Invalid migration identity")
    timestamp(value["created_at"])
    MigrationInput.model_validate(value["input"])
    return value


def apply(path, manifest_file, operation_id, *, service_stopped=False):
    """Apply or resume one saved plan while runtime access remains blocked.

    Candidate records, the immutable migration receipt and signed checkpoint are
    one transaction. A crash before commit rolls everything back; after commit
    the same manifest only republishes the anchor, never reimports candidates.
    """
    if not service_stopped:
        raise ValueError("Candidate migration requires an explicitly stopped service")
    store = _store(path)
    manifest = _load_manifest(manifest_file, store.path, operation_id)
    principal = manifest["principal_id"]
    receipt_id = uid("knowledge_migration", principal, operation_id)
    marker = store.knowledge_migration_marker
    marker_data = {"operation_id": operation_id, "database": str(store.path),
                   "manifest_digest": manifest["manifest_digest"]}
    pending = marker.exists()
    if pending and json.loads(marker.read_text(encoding="utf-8")) != marker_data:
        raise ValueError("Pending candidate migration belongs to a different saved manifest")
    con = _connect(store.path, readonly=True)
    try:
        con.execute("BEGIN")
        _verify(store, con, SCHEMA_VERSION)
        receipt = con.execute("SELECT * FROM records WHERE id=?", (receipt_id,)).fetchone()
        if receipt is not None:
            stored = store.decode(receipt)
            if stored["kind"] != "knowledge_migration" or stored["data"]["manifest_digest"] != manifest["manifest_digest"]:
                raise Conflict("Operation ID already belongs to a different migration manifest")
            if not pending:
                return {**stored["data"]["report"], "already_applied": True, "pending": False}
        elif _baseline(con) != manifest["baseline"]:
            raise Conflict("Database changed since dry-run; prepare a new explicit plan")
    finally:
        con.close()
    if not pending:
        save_manifest(marker_data, marker)
    service, Identity = _service(store)
    identity = Identity(principal, HOST, frozenset({"write"}))
    checkpoint_reason = "knowledge_candidate_migration:" + receipt_id
    con = _connect(store.path)
    try:
        con.execute("BEGIN EXCLUSIVE")
        _verify(store, con, SCHEMA_VERSION)
        existing = con.execute("SELECT * FROM records WHERE id=?", (receipt_id,)).fetchone()
        if existing is None:
            if _baseline(con) != manifest["baseline"]:
                raise Conflict("Database changed before the offline transaction")
            if any(json.loads(row[0]).get("principal_digest") == digest(principal)
                   for row in con.execute("SELECT data_json FROM records WHERE kind='erasure'")):
                raise Conflict("Erased principal is revoked")
            inputs = MigrationInput.model_validate(manifest["input"])
            entries = [_entry(con, service, identity, operation_id, number, item)
                       for number, item in enumerate(inputs.items)]
            if canonical(entries) != canonical(manifest["entries"]):
                raise Conflict("Saved mapping no longer matches its canonical inputs")
            created_sources, results = set(), []
            for entry in entries:
                if entry["status"] != "ready":
                    continue
                for source_id, value in entry["source_requests"].items():
                    if source_id not in created_sources:
                        source = service.knowledge.register_source_in_transaction(con, identity, SourceCreate.model_validate(value))
                        if source["id"] != source_id:
                            raise Conflict("Source identity differs from the saved manifest")
                        created_sources.add(source_id)
                result = service.extraction.submit_in_transaction(con, identity,
                    ExtractionSubmit.model_validate(entry["extraction_request"]))
                expected_refs = {(entry["planned_claim_ref"]["id"], 1, "knowledge_claim"),
                                 (entry["planned_extraction_ref"]["id"], 1, "knowledge_extraction")}
                actual_refs = {(ref["id"], ref["version"], ref["kind"]) for ref in result["records"]}
                if expected_refs != actual_refs:
                    raise Conflict("Candidate/extraction identities differ from the saved manifest")
                results.append({"memory_ref": entry["memory_ref"], "result": result})
            _preserved(con, manifest["baseline"])
            report = {"operation_id": operation_id, "manifest_digest": manifest["manifest_digest"],
                      "applied": True, "pending": False, "already_applied": False,
                      "ready_count": manifest["ready_count"], "quarantined_count": manifest["quarantined_count"],
                      "source_count": len(created_sources), "mappings": results,
                      "candidate_count": len({ref["id"] for item in results for ref in item["result"]["records"]
                                              if ref["kind"] == "knowledge_claim"}),
                      "extraction_count": len({ref["id"] for item in results for ref in item["result"]["records"]
                                               if ref["kind"] == "knowledge_extraction"}),
                      # The offline plan may name inaccessible input IDs. Only
                      # item positions/reasons belong in this owner's API view.
                      "quarantined": [{"item": entry["item"], "reason": entry["reason"]}
                                      for entry in entries if entry["status"] == "quarantined"],
                      "existing_records_preserved_at_commit": True,
                      "current_applicability": "not_evaluated"}
            store.put(con, identity=receipt_id, principal=principal, kind="knowledge_migration", conversation=None,
                data={"operation_id": operation_id, "manifest_digest": manifest["manifest_digest"],
                      # The saved offline manifest contains a global baseline.
                      # Never copy other principals' IDs/digests into this receipt.
                      "report": report,
                      "provenance": provenance("explicit_offline_operator", "semantic_memory_candidates_migrated",
                          target="candidate_migration", causes=[entry["memory_ref"]["id"]
                              for entry in entries if entry["status"] == "ready"], policy=FORMAT)},
                reason="knowledge_candidates_migrated")
            checkpoint = store.security.create_checkpoint(con, reason=checkpoint_reason)
            checkpoint_id = checkpoint["checkpoint_id"]
        else:
            stored = store.decode(existing)
            if stored["kind"] != "knowledge_migration" or stored["data"]["manifest_digest"] != manifest["manifest_digest"]:
                raise Conflict("Committed migration has a different manifest")
            _preserved(con, manifest["baseline"])
            report = {**stored["data"]["report"], "already_applied": True}
            checkpoint = con.execute("SELECT checkpoint_id FROM audit_checkpoints WHERE json_extract(payload_json,'$.reason')=?",
                                     (checkpoint_reason,)).fetchone()
            if checkpoint is None:
                raise ValueError("Committed migration checkpoint is missing")
            checkpoint_id = checkpoint["checkpoint_id"]
        anchor = store.security.anchor_document(con, checkpoint_id)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    store.security.write_anchor(anchor)
    con = _connect(store.path, readonly=True)
    try:
        _verify(store, con, SCHEMA_VERSION)
        _preserved(con, manifest["baseline"])
        if store.security.read_anchor()["checkpoint"]["checkpoint_id"] != checkpoint_id:
            raise ValueError("Migration anchor publication could not be verified")
    finally:
        con.close()
    marker.unlink()
    return {**report, "checkpoint_id": checkpoint_id}

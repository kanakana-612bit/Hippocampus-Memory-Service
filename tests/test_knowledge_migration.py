"""Fictional offline K2 migration fixtures; never open a deployment database."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from v02.api import Identity, create_app
from checkpoint_security import CheckpointSecurity
from v02.contracts import digest
from v02.extraction_service import ExtractionService
from v02.knowledge_migration import apply, plan, save_manifest
from v02.service import Service
from v02.storage import Conflict, Storage


ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp-tests"


class KnowledgeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = TMP / ("knowledge-migration-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(self.clean_fixture)
        self.environment = patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = self.folder / "demo.db"
        self.service = Service(self.path)
        self.principal = "demo:migration-owner"
        self.identity = Identity(self.principal, "demo:host", frozenset({"write", "read", "review", "sync"}))
        self.events = [
            {"id": "demo:observation", "role": "user", "actor_id": "demo:observer",
             "content": "Demo beacon is blue. 🌟"},
            {"id": "demo:assistant", "role": "assistant", "actor_id": "demo:model",
             "model_provenance": {"provider": "fictional-model"},
             "content": "Demo summary repeats the blue beacon report."},
            {"id": "demo:off-path", "role": "user", "actor_id": "demo:observer",
             "content": "A different demo branch describes a green beacon."},
            {"id": "demo:story", "role": "user", "sandbox_id": "demo:fiction",
             "content": "The fictional beacon is made of stars."},
        ]
        self.service.sync(self.principal, {"conversation_id": "demo:chat", "messages": self.events,
            "active_event_ids": ["demo:observation", "demo:assistant", "demo:story"],
            "revision_key": "demo:r1", "expected_revision": None, "auto_capture": False})
        self.memory = self.make_memory("demo:semantic", ["demo:observation"])
        self.manifest = self.folder / "saved-plan.json"
        self.operation = "demo:explicit-import"

    def clean_fixture(self):
        resolved = self.folder.resolve()
        self.assertEqual(resolved.parent, TMP.resolve())
        self.assertTrue(resolved.name.startswith("knowledge-migration-"))
        if resolved.exists():
            shutil.rmtree(resolved)

    def make_memory(self, identity, refs, **changes):
        return self.service.create_memory(self.principal, {"idempotency_key": "create:" + identity,
            "reason": "Fictional semantic memory fixture", "memory": {
                "id": identity, "conversation_id": "demo:chat", "memory_type": "semantic",
                "content": "Demo memory contains a reported claim, not certified truth.",
                "source_event_ids": refs, "retrieval_weight": .2, "salience": .95,
                "epistemic_status": "confirmed", **changes}})

    def item(self, memory=None, event="demo:observation"):
        memory = memory or self.memory
        source = self.service.get(self.principal, event)
        content = source["data"]["content"]
        return {"memory_ref": {"id": memory["id"], "version": memory["version"]},
            "memory_digest": digest(memory["data"]),
            "scope": {"conversation_id": "demo:chat"}, "path_revision": "demo:r1",
            "proposition": {"text": "A demo narrator reports a blue beacon.", "kind": "attributed_report",
                "subject": "demo:observer", "predicate": "reports", "object": "blue beacon",
                "attribution_actor_id": "demo:observer", "polarity": "affirmed", "quantifier": "single"},
            "spans": [{"event_ref": {"id": event, "version": source["version"]},
                       "event_digest": digest(content), "start": 0, "end": len(content), "quote": content}],
            "confidence": {"target": "claim_extraction", "score": .99, "calibrated": False},
            "reason": "Explicit fictional mapping; extraction is not truth review"}

    def prepare(self, items=None, operation=None, filename=None):
        value = plan(self.path, self.principal, operation or self.operation,
                     {"items": items or [self.item()]})
        save_manifest(value, filename or self.manifest)
        return value

    def apply(self, **changes):
        return apply(self.path, self.manifest, self.operation, service_stopped=True, **changes)

    def raw_state(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            return {"records": [dict(row) for row in con.execute("SELECT * FROM records ORDER BY id")],
                    "audit": [dict(row) for row in con.execute("SELECT * FROM audit_events ORDER BY sequence")],
                    "memory_fts": [tuple(row) for row in con.execute("SELECT id,content,keywords FROM memory_fts ORDER BY id")],
                    "checkpoint_count": con.execute("SELECT count(*) FROM audit_checkpoints").fetchone()[0]}
        finally:
            con.close()

    def test_plan_is_read_only_and_apply_preserves_old_memory_and_confidence(self):
        self.memory = self.service.update_memory(self.principal, self.memory["id"], {
            "expected_version": self.memory["version"], "idempotency_key": "demo:old-assessment",
            "reason": "Fictional preexisting review", "assessment": {"target": "world_claim",
                "claim": self.memory["data"]["content"], "status": "assessed", "score": .98,
                "evidence_refs": ["demo:observation"]}}, "assessment")
        before = self.raw_state()
        file_digest = digest(self.path.read_bytes().hex())
        manifest = self.prepare()
        self.assertEqual(manifest["ready_count"], 1)
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(digest(self.path.read_bytes().hex()), file_digest)
        result = self.apply()
        self.assertTrue(result["existing_records_preserved_at_commit"])
        after = self.raw_state()
        after_by_id = {row["id"]: row for row in after["records"]}
        for row in before["records"]:
            self.assertEqual(after_by_id[row["id"]], row)
        self.assertEqual(after["memory_fts"], before["memory_fts"])
        self.assertEqual(after["audit"][:len(before["audit"])], before["audit"])
        claims = [json.loads(row["data_json"]) for row in after["records"] if row["kind"] == "knowledge_claim"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["knowledge_state"], "provisional")
        self.assertEqual(claims[0]["assessment"]["status"], "unknown")
        self.assertIsNone(claims[0]["assessment"]["score"])
        self.assertEqual(claims[0]["source_independence"], "unknown")
        self.assertEqual(self.service.get(self.principal, self.memory["id"])["data"], self.memory["data"])
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_quarantine_untraceable_off_path_fiction_and_bad_mapping(self):
        untraceable = self.make_memory("demo:no-origin", [])
        off_path = self.make_memory("demo:old-branch", ["demo:off-path"])
        fiction = self.make_memory("demo:fiction-memory", ["demo:story"], sandbox_id="demo:fiction", usage_scope="fiction")
        episodic = self.make_memory("demo:episode", ["demo:observation"], memory_type="episodic")
        wrong_digest = self.item()
        wrong_digest["spans"][0]["event_digest"] = "0" * 64
        wrong_span = self.item()
        wrong_span["spans"][0]["quote"] = "Not the original demo quote"
        items = [self.item(untraceable), self.item(off_path, "demo:off-path"),
                 self.item(fiction, "demo:story"), self.item(episodic), wrong_digest, wrong_span]
        manifest = self.prepare(items)
        reasons = {entry["reason"] for entry in manifest["entries"]}
        self.assertEqual(reasons, {"original_events_untraceable", "lineage_event_off_path",
            "lineage_fiction_mismatch", "memory_not_semantic", "event_digest_mismatch", "span_quote_mismatch"})
        self.assertEqual(manifest["ready_count"], 0)
        self.assertEqual(self.apply()["quarantined_count"], 6)
        self.assertFalse(any(row["kind"] == "knowledge_claim" for row in self.raw_state()["records"]))

    def test_parent_memory_lineage_and_model_output_are_retained(self):
        parent = self.make_memory("demo:parent", ["demo:observation"])
        child = self.make_memory("demo:child", ["demo:assistant"], source_memory_ids=[parent["id"]])
        source = self.service.knowledge.create_source(self.identity, {"idempotency_key": "demo:parent-source",
            "reason": "Demo original report", "path_revision": "demo:r1",
            "scope": {"conversation_id": "demo:chat"}, "event_ref": {"id": "demo:observation", "version": 1}})
        ref = next(row for row in source["records"] if row["kind"] == "knowledge_source")
        item = self.item(child, "demo:assistant")
        item["spans"][0]["parent_source_refs"] = [{"id": ref["id"], "version": ref["version"]}]
        self.prepare([item])
        self.apply()
        rows = self.raw_state()["records"]
        extraction = json.loads(next(row["data_json"] for row in rows if row["kind"] == "knowledge_extraction"))
        span = extraction["spans"][0]
        self.assertEqual(span["role"], "assistant")
        self.assertEqual(span["origin_status"], "unknown")
        self.assertEqual(span["origin_ids"], ["demo:observation"])
        self.assertEqual({row["id"] for row in span["event_refs"]}, {"demo:observation", "demo:assistant"})
        self.assertEqual(span["model_provenance"], {"provider": "fictional-model"})

    def test_migration_cannot_omit_an_existing_sources_known_parent(self):
        def source(key, event, parents=()):
            result = self.service.knowledge.create_source(self.identity, {"idempotency_key": key,
                "reason": "Fictional previously known lineage", "path_revision": "demo:r1",
                "scope": {"conversation_id": "demo:chat"}, "event_ref": {"id": event, "version": 1},
                "parent_source_refs": list(parents)})
            ref = next(row for row in result["records"] if row["kind"] == "knowledge_source")
            return {"id": ref["id"], "version": ref["version"]}
        parent = source("demo:known-parent", "demo:observation")
        source("demo:known-derived", "demo:assistant", [parent])
        memory = self.make_memory("demo:derived-memory", ["demo:observation", "demo:assistant"])
        missing = self.item(memory, "demo:assistant")
        preserved = deepcopy(missing)
        preserved["spans"][0]["parent_source_refs"] = [parent]
        manifest = self.prepare([missing, preserved])
        self.assertEqual(manifest["entries"][0]["reason"], "known_source_lineage_omitted_or_changed")
        self.assertEqual(manifest["entries"][1]["status"], "ready")
        result = self.apply()
        self.assertEqual(result["candidate_count"], 1)
        extraction = next(json.loads(row["data_json"]) for row in self.raw_state()["records"]
                          if row["kind"] == "knowledge_extraction")
        self.assertEqual(extraction["spans"][0]["origin_ids"], ["demo:observation"])

    def test_apply_requires_saved_manifest_same_operation_and_stopped_service(self):
        manifest = self.prepare()
        with self.assertRaises(ValueError):
            apply(self.path, self.manifest, self.operation)
        with self.assertRaises(ValueError):
            apply(self.path, self.manifest, "demo:other-operation", service_stopped=True)
        changed = deepcopy(manifest)
        changed["input"]["items"][0]["reason"] = "Modified after dry-run"
        self.manifest.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.apply()
        self.assertFalse(self.service.db.knowledge_migration_marker.exists())

    def test_changed_database_or_path_requires_new_plan(self):
        self.prepare()
        self.service.sync(self.principal, {"conversation_id": "demo:chat", "messages": [],
            "active_event_ids": ["demo:assistant"], "revision_key": "demo:r2",
            "expected_revision": "demo:r1", "auto_capture": False})
        with self.assertRaisesRegex(Conflict, "changed since dry-run"):
            self.apply()
        self.assertFalse(self.service.db.knowledge_migration_marker.exists())

    def test_failure_before_commit_rolls_back_and_same_manifest_resumes(self):
        self.prepare()
        before = self.raw_state()
        original = ExtractionService.submit_in_transaction
        def fail_after_writes(extractor, *args):
            original(extractor, *args)
            raise RuntimeError("Fictional failure before outer commit")
        with patch.object(ExtractionService, "submit_in_transaction", fail_after_writes):
            with self.assertRaisesRegex(RuntimeError, "before outer commit"):
                self.apply()
        self.assertEqual(self.raw_state(), before)
        self.assertTrue(self.service.db.knowledge_migration_marker.exists())
        with self.assertRaises(ValueError):
            Storage(self.path)
        self.assertTrue(self.apply()["applied"])
        self.assertFalse(self.service.db.knowledge_migration_marker.exists())
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_anchor_failure_resumes_without_reimport_or_extra_checkpoint(self):
        self.prepare()
        with patch.object(CheckpointSecurity, "write_anchor", side_effect=OSError("Fictional anchor failure")):
            with self.assertRaisesRegex(OSError, "anchor failure"):
                self.apply()
        committed = self.raw_state()
        self.assertEqual(sum(row["kind"] == "knowledge_claim" for row in committed["records"]), 1)
        with self.assertRaises(ValueError):
            Storage(self.path)
        with patch.object(ExtractionService, "submit_in_transaction", side_effect=AssertionError("Do not reimport")):
            resumed = self.apply()
        self.assertTrue(resumed["already_applied"])
        self.assertEqual(self.raw_state(), committed)
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_replay_is_read_only_even_after_later_legitimate_changes(self):
        self.prepare()
        result = self.apply()
        self.service.update_memory(self.principal, self.memory["id"], {"idempotency_key": "demo:later-change",
            "reason": "Explicit later fixture update", "expected_version": 1, "salience": .4}, "weights")
        before = self.raw_state()
        replay = self.apply()
        self.assertTrue(replay["already_applied"])
        self.assertEqual(replay["mappings"], result["mappings"])
        self.assertEqual(self.raw_state(), before)

    def test_pending_resume_rejects_different_manifest_and_operation_reuse(self):
        first = self.prepare()
        second_file = self.folder / "second-plan.json"
        altered = self.item()
        altered["reason"] = "Different explicit proposal review reason"
        second = plan(self.path, self.principal, self.operation, {"items": [altered]})
        save_manifest(second, second_file)
        with patch.object(CheckpointSecurity, "write_anchor", side_effect=OSError("demo")):
            with self.assertRaises(OSError):
                self.apply()
        with self.assertRaisesRegex(ValueError, "different saved manifest"):
            apply(self.path, second_file, self.operation, service_stopped=True)
        self.apply()
        with self.assertRaisesRegex(Conflict, "different migration manifest"):
            apply(self.path, second_file, self.operation, service_stopped=True)
        self.assertNotEqual(first["manifest_digest"], second["manifest_digest"])

    def test_saved_plan_is_never_silently_overwritten(self):
        manifest = self.prepare()
        with self.assertRaises(FileExistsError):
            save_manifest(manifest, self.manifest)

    def test_manifest_pins_planned_ids_and_reports_unique_candidates(self):
        item = self.item()
        manifest = self.prepare([item, deepcopy(item)])
        self.assertEqual(manifest["ready_count"], 2)
        first, second = manifest["entries"]
        self.assertEqual(first["planned_claim_ref"], second["planned_claim_ref"])
        self.assertEqual(first["planned_extraction_ref"], second["planned_extraction_ref"])
        result = self.apply()
        self.assertEqual(result["ready_count"], 2)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["extraction_count"], 1)
        self.assertEqual(result["source_count"], 1)
        for mapping in result["mappings"]:
            records = mapping["result"]["records"]
            self.assertIn({**first["planned_claim_ref"], "kind": "knowledge_claim"}, records)
            self.assertIn({**first["planned_extraction_ref"], "kind": "knowledge_extraction"}, records)

    def test_api_visible_receipt_does_not_copy_another_owners_baseline(self):
        other = "demo:private-other-owner"
        self.service.sync(other, {"conversation_id": "demo:other-chat", "messages": [{
            "id": "demo:other-private-event", "role": "user", "content": "Private fictional other-owner text"}],
            "active_event_ids": ["demo:other-private-event"], "revision_key": "other:r1",
            "expected_revision": None, "auto_capture": False})
        other_event = self.service.get(other, "demo:other-private-event")
        manifest = self.prepare()
        self.assertTrue(any(row["id"] == other_event["id"] for row in manifest["baseline"]["records"]))
        self.apply()
        receipt = next(row for row in self.raw_state()["records"] if row["kind"] == "knowledge_migration")
        credentials = [{"token_sha256": hashlib.sha256(b"fictional-migration-reader").hexdigest(),
                        "principal_id": self.principal, "host_id": "demo:reader", "permissions": ["read"]}]
        with TestClient(create_app(self.service, credentials)) as client:
            response = client.get("/api/v0.2/records/" + receipt["id"],
                headers={"Authorization": "Bearer fictional-migration-reader"})
        self.assertEqual(response.status_code, 200)
        visible = response.json()
        text = json.dumps(visible, ensure_ascii=False)
        self.assertNotIn(other_event["id"], text)
        self.assertNotIn(other, text)
        self.assertNotIn(other_event["data"]["content"], text)
        self.assertNotIn(digest(other_event["data"]), text)
        other_baseline = next(row for row in manifest["baseline"]["records"] if row["id"] == other_event["id"])
        self.assertNotIn(other_baseline["digest"], text)
        self.assertNotIn("baseline", visible["data"])
        self.assertNotIn("manifest", visible["data"])


if __name__ == "__main__":
    unittest.main()

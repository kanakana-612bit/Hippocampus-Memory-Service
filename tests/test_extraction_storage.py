"""Fictional schema-3 extraction storage and offline-upgrade fixtures only."""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

import test_knowledge_storage as knowledge_tests
from v02.contracts import canonical, provenance, uid
from v02.maintenance import erase_principal, restore
from v02.schema_upgrade import _apply_step, upgrade
from v02.storage import Conflict, SCHEMA_VERSION, Storage, inspect_schema


class ExtractionStorageTests(unittest.TestCase):
    setUp = knowledge_tests.KnowledgeStorageTests.setUp
    tearDown = knowledge_tests.KnowledgeStorageTests.tearDown
    schema_one = knowledge_tests.KnowledgeStorageTests.schema_one
    rows = knowledge_tests.KnowledgeStorageTests.rows

    def schema_two(self):
        store = self.schema_one()
        upgrade(self.path, "fictional-k1", target_schema="2", apply=True, service_stopped=True)
        return store

    def append(self, store, kind, identity, principal="fictional-owner"):
        with store.connection(write=True) as con:
            return store.put(con, identity=identity, principal=principal, kind=kind,
                             conversation="fictional-chat", data={"fictional": True, "claim_ids": []},
                             reason="fictional_fixture")

    def old_k1_receipt(self, *, pending=False):
        """Frozen pre-K2 receipt/marker shape: no steps or target-aware command API."""
        store = self.schema_one()
        with store.connection(write=True) as con:
            _apply_step(con, "2")
            receipt = store.put(con, identity=uid("schema_upgrade", "original-k1"), principal="$offline-maintenance",
                                kind="schema_upgrade", conversation=None,
                                data={"operation_id": "original-k1", "from_schema": "1", "to_schema": "2",
                                      "provenance": provenance("explicit_offline_operator", "canonical_schema_upgrade")},
                                reason="canonical_schema_upgrade")
            checkpoint = store.security.create_checkpoint(con, reason="canonical_schema_upgrade:original-k1")
            anchor = store.security.anchor_document(con, checkpoint["checkpoint_id"])
        if pending:
            store.schema_upgrade_marker.write_text(canonical({"operation_id": "original-k1", "database": str(self.path.resolve()),
                                                              "from_schema": "1", "to_schema": "2"}), encoding="utf-8")
        else:
            store.security.write_anchor(anchor)
        return store, receipt

    def test_schema_two_startup_dryrun_and_missing_stop_confirmation_are_readonly(self):
        store = self.schema_two()
        before, anchor = self.path.read_bytes(), store.security.anchor_path.read_bytes()
        with self.assertRaises(ValueError):
            Storage(self.path)
        plan = upgrade(self.path, "fictional-k2")
        self.assertEqual(plan["steps"], [{"from_schema": str(v), "to_schema": str(v + 1)}
                                        for v in range(2, int(SCHEMA_VERSION))])
        self.assertFalse(plan["applied"])
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-k2", apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.security.anchor_path.read_bytes(), anchor)
        self.assertFalse(store.schema_upgrade_marker.exists())

    def test_schema_two_upgrade_preserves_records_audit_prefix_and_existing_fts(self):
        store = self.schema_two()
        with store.connection(write=True) as con:
            store.put(con, identity="fictional-claim", principal="fictional-owner", kind="knowledge_claim", conversation=None,
                      data={"proposition": {"text": "The fictional beacon is violet."}}, reason="fictional_fixture")
        records, audit = self.rows("records"), self.rows("audit_events")
        result = upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        self.assertEqual(result["from_schema"], "2")
        self.assertEqual(result["to_schema"], SCHEMA_VERSION)
        self.assertEqual(self.rows("records")[:len(records)], records)
        self.assertEqual(self.rows("audit_events")[:len(audit)], audit)
        reopened = Storage(self.path)
        self.assertTrue(reopened.verify()["valid"])
        with reopened.connection() as con:
            self.assertEqual(con.execute("SELECT content FROM knowledge_fts WHERE id='fictional-claim'").fetchone()[0],
                             "The fictional beacon is violet.")

    def test_schema_one_to_latest_records_all_steps_in_one_receipt(self):
        self.schema_one()
        audit = self.rows("audit_events")
        result = upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        self.assertEqual(result["steps"], [{"from_schema": str(v), "to_schema": str(v + 1)}
                                          for v in range(1, int(SCHEMA_VERSION))])
        self.assertEqual(self.rows("audit_events")[:len(audit)], audit)
        store = Storage(self.path)
        with store.connection() as con:
            receipts = store.list(con, "$offline-maintenance", "schema_upgrade")
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["data"]["steps"], result["steps"])

    def test_original_k1_receipt_keeps_identity_and_cannot_be_reused_for_three(self):
        _, original = self.old_k1_receipt()
        before = self.path.read_bytes()
        self.assertTrue(upgrade(self.path, "original-k1", target_schema="2")["already_applied"])
        with self.assertRaises(ValueError):
            upgrade(self.path, "original-k1", apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        upgrade(self.path, "new-k2", apply=True, service_stopped=True)
        store = Storage(self.path)
        with store.connection() as con:
            self.assertEqual(store.get(con, "$offline-maintenance", original["id"]), original)
        before = self.path.read_bytes()
        replay = upgrade(self.path, "original-k1", target_schema="2", apply=True)
        self.assertTrue(replay["already_applied"])
        self.assertEqual(replay["current_schema"], SCHEMA_VERSION)
        self.assertEqual(self.path.read_bytes(), before)

    def test_original_pending_k1_operation_resumes_only_its_original_target(self):
        store, original = self.old_k1_receipt(pending=True)
        before = self.rows("audit_events")
        with self.assertRaises(ValueError):
            upgrade(self.path, "original-k1", apply=True, service_stopped=True)
        self.assertTrue(store.schema_upgrade_marker.exists())
        result = upgrade(self.path, "original-k1", target_schema="2", apply=True, service_stopped=True)
        self.assertTrue(result["already_applied"])
        self.assertEqual(inspect_schema(self.path), "2")
        self.assertEqual(self.rows("audit_events"), before)
        self.assertFalse(store.schema_upgrade_marker.exists())
        with self.assertRaises(ValueError):
            Storage(self.path)
        upgrade(self.path, "new-k2", apply=True, service_stopped=True)
        with Storage(self.path).connection() as con:
            self.assertEqual(json.loads(con.execute("SELECT data_json FROM records WHERE id=?", (original["id"],)).fetchone()[0]), original["data"])

    def test_two_to_latest_anchor_failure_blocks_runtime_and_resumes_once(self):
        store = self.schema_two()
        with patch("checkpoint_security.CheckpointSecurity.write_anchor", side_effect=OSError("fictional interruption")):
            with self.assertRaises(OSError):
                upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), SCHEMA_VERSION)
        before = self.rows("audit_events")
        with self.assertRaises(ValueError):
            Storage(self.path)
        with self.assertRaises(ValueError), store.connection():
            pass
        result = upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        self.assertTrue(result["already_applied"])
        self.assertEqual(self.rows("audit_events"), before)
        checkpoint_count = len(self.rows("audit_checkpoints"))
        self.assertTrue(upgrade(self.path, "fictional-k2", apply=True)["already_applied"])
        self.assertEqual(len(self.rows("audit_checkpoints")), checkpoint_count)

    def test_intermediate_step_failure_rolls_back_entire_one_to_three_operation(self):
        self.schema_one()
        before = self.rows("audit_events")
        def interrupt_after_two(con, target):
            if target == "3":
                raise RuntimeError("fictional interruption between steps")
            _apply_step(con, target)
        with patch("v02.schema_upgrade._apply_step", side_effect=interrupt_after_two):
            with self.assertRaises(RuntimeError):
                upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), "1")
        self.assertEqual(self.rows("audit_events"), before)
        self.assertTrue(upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)["applied"])

    def test_schema_two_index_tampering_blocks_upgrade_without_marker(self):
        store = self.schema_two()
        with store.connection(write=True) as con:
            con.execute("INSERT INTO knowledge_fts(id,content) VALUES ('unowned','fictional forged index')")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(store.schema_upgrade_marker.exists())

    def test_extraction_and_migration_records_are_immutable_and_restore_exactly(self):
        store = Storage(self.path)
        records = [self.append(store, kind, kind) for kind in ("knowledge_extraction", "knowledge_migration")]
        for row in records:
            with self.subTest(kind=row["kind"]):
                with self.assertRaises(Conflict), store.connection(write=True) as con:
                    store.put(con, identity=row["id"], principal=row["principal_id"], kind=row["kind"], conversation=row["conversation_id"],
                              data={"fictional": False}, expected=1, reason="fictional_mutation")
                with self.assertRaises(sqlite3.IntegrityError), store.connection(write=True) as con:
                    con.execute("UPDATE records SET data_json='{}' WHERE id=?", (row["id"],))
        backup = store.backup("fictional-k2")
        self.append(store, "knowledge_extraction", "later-extraction")
        self.assertTrue(restore(self.path, backup["filename"], apply=True, service_stopped=True)["verification"]["valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            for row in records:
                self.assertEqual(reopened.get(con, row["principal_id"], row["id"]), row)

    def test_restore_rejects_schema_two_backup_before_any_checkpoint(self):
        store = self.schema_two()
        backup = store.backup("fictional-schema-two")
        upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            restore(self.path, backup["filename"], apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(store.maintenance_marker.exists())

    def test_principal_erasure_removes_extraction_and_migration_but_preserves_other_owner(self):
        store = Storage(self.path)
        survivors = []
        for kind in ("knowledge_extraction", "knowledge_migration"):
            self.append(store, kind, "erase-" + kind)
            survivors.append(self.append(store, kind, "keep-" + kind, principal="other-fictional-owner"))
        result = erase_principal(self.path, "fictional-owner", "fictional-erasure", apply=True,
                                 service_stopped=True, confirm_principal="fictional-owner")
        self.assertTrue(result["verification"]["valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM records WHERE principal_id='fictional-owner'").fetchone()[0], 0)
            for row in survivors:
                self.assertEqual(reopened.get(con, row["principal_id"], row["id"]), row)

    def test_pending_knowledge_migration_blocks_runtime_maintenance_and_schema_upgrade(self):
        store = Storage(self.path)
        backup = store.backup("fictional-before-pending")
        store.knowledge_migration_marker.write_text(canonical({"operation_id": "fictional-migration"}), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            Storage(self.path)
        with self.assertRaises(ValueError), store.connection():
            pass
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-schema-operation")
        with self.assertRaises(ValueError):
            restore(self.path, backup["filename"])
        with self.assertRaises(ValueError):
            erase_principal(self.path, "fictional-owner", "fictional-erasure")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

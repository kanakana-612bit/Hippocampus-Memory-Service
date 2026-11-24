"""Fictional schema-4 search history, temporary lease, and upgrade fixtures only."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

import test_knowledge_storage as knowledge_tests
from v02.maintenance import erase_principal, restore
from v02.schema_upgrade import _apply_step, upgrade
from v02.storage import Conflict, KNOWLEDGE_IMMUTABLE_KINDS, SCHEMA_VERSION, Storage, inspect_schema


HISTORY_KINDS = ("knowledge_search_plan", "knowledge_search_attempt", "knowledge_search_observation")
LEASE_KIND = "knowledge_search_lease"


class CountersearchStorageTests(unittest.TestCase):
    setUp = knowledge_tests.KnowledgeStorageTests.setUp
    tearDown = knowledge_tests.KnowledgeStorageTests.tearDown
    schema_one = knowledge_tests.KnowledgeStorageTests.schema_one
    rows = knowledge_tests.KnowledgeStorageTests.rows

    def prior_schema(self, version="3"):
        store = self.schema_one()
        if version != "1":
            upgrade(self.path, "fictional-prior-" + version, target_schema=version, apply=True, service_stopped=True)
        return store

    def record(self, store, kind, identity, *, principal="fictional-owner", expected=None, active=True):
        with store.connection(write=True) as con:
            return store.put(con, identity=identity, principal=principal, kind=kind,
                             conversation="fictional-chat",
                             data={"fictional": True, "scope": {"conversation_id": "fictional-chat"},
                                   "expiry": "2000-01-01T00:00:00+00:00", "active": active},
                             reason="fictional_fixture", expected=expected)

    def test_schema_three_startup_and_upgrade_plan_do_not_write(self):
        store = self.prior_schema()
        before, anchor = self.path.read_bytes(), store.security.anchor_path.read_bytes()
        with self.assertRaises(ValueError):
            Storage(self.path)
        plan = upgrade(self.path, "fictional-k3")
        self.assertEqual(plan["steps"], [{"from_schema": str(v), "to_schema": str(v + 1)}
                                        for v in range(3, int(SCHEMA_VERSION))])
        self.assertFalse(plan["applied"])
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-k3", apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.security.anchor_path.read_bytes(), anchor)
        self.assertFalse(store.schema_upgrade_marker.exists())

    def test_all_prior_versions_upgrade_with_audit_and_record_prefix_intact(self):
        for version in ("1", "2", "3"):
            with self.subTest(version=version):
                self.path = self.folder / ("fictional-schema-" + version + ".db")
                self.prior_schema(version)
                records, events = self.rows("records"), self.rows("audit_events")
                result = upgrade(self.path, "fictional-k3", target_schema="4", apply=True, service_stopped=True)
                self.assertEqual(result["steps"], [{"from_schema": str(v), "to_schema": str(v + 1)}
                                                  for v in range(int(version), 4)])
                self.assertEqual(self.rows("records")[:len(records)], records)
                self.assertEqual(self.rows("audit_events")[:len(events)], events)
                self.assertEqual(inspect_schema(self.path), "4")
                with self.assertRaises(ValueError):
                    Storage(self.path)
                schema_four_records, schema_four_events = self.rows("records"), self.rows("audit_events")
                current = upgrade(self.path, "fictional-current", apply=True, service_stopped=True)
                self.assertEqual(current["from_schema"], "4")
                self.assertEqual(current["to_schema"], SCHEMA_VERSION)
                self.assertEqual(self.rows("records")[:len(schema_four_records)], schema_four_records)
                self.assertEqual(self.rows("audit_events")[:len(schema_four_events)], schema_four_events)
                self.assertTrue(Storage(self.path).verify()["valid"])

    def test_old_targets_replay_after_four_without_relabel_or_anchor_rollback(self):
        store = self.schema_one()
        for target in ("2", "3"):
            upgrade(self.path, "fictional-target-" + target, target_schema=target, apply=True, service_stopped=True)
        with store.connection() as con:
            originals = store.list(con, "$offline-maintenance", "schema_upgrade")
        upgrade(self.path, "fictional-k3", target_schema="4", apply=True, service_stopped=True)
        with self.assertRaises(ValueError):
            Storage(self.path)
        # Check the original schema-4 guarantee, then replay the same immutable
        # 2/3/4 receipts again after a separately authorized current upgrade.
        for current_schema in ("4", SCHEMA_VERSION):
            if current_schema != "4":
                upgrade(self.path, "fictional-current", apply=True, service_stopped=True)
            before, anchor = self.path.read_bytes(), store.security.anchor_path.read_bytes()
            for target in ("2", "3", "4"):
                operation = "fictional-k3" if target == "4" else "fictional-target-" + target
                replay = upgrade(self.path, operation, target_schema=target, apply=True)
                self.assertTrue(replay["already_applied"])
                self.assertEqual(replay["to_schema"], target)
                self.assertEqual(replay["current_schema"], current_schema)
                with self.assertRaises(ValueError):
                    upgrade(self.path, operation, target_schema=SCHEMA_VERSION, apply=True, service_stopped=True)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(store.security.anchor_path.read_bytes(), anchor)
        with Storage(self.path).connection() as con:
            for original in originals:
                self.assertEqual(store.get(con, "$offline-maintenance", original["id"]), original)

    def test_pending_target_three_resumes_before_a_separate_target_four_operation(self):
        store = self.prior_schema("2")
        with patch("checkpoint_security.CheckpointSecurity.write_anchor", side_effect=OSError("fictional interruption")):
            with self.assertRaises(OSError):
                upgrade(self.path, "fictional-k2", target_schema="3", apply=True, service_stopped=True)
        events = self.rows("audit_events")
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-k2", apply=True, service_stopped=True)
        result = upgrade(self.path, "fictional-k2", target_schema="3", apply=True, service_stopped=True)
        self.assertTrue(result["already_applied"])
        self.assertEqual(inspect_schema(self.path), "3")
        self.assertEqual(self.rows("audit_events"), events)
        self.assertFalse(store.schema_upgrade_marker.exists())
        with self.assertRaises(ValueError):
            Storage(self.path)
        self.assertTrue(upgrade(self.path, "fictional-k3", target_schema="4", apply=True, service_stopped=True)["applied"])
        self.assertEqual(inspect_schema(self.path), "4")
        with self.assertRaises(ValueError):
            Storage(self.path)
        self.assertTrue(upgrade(self.path, "fictional-current", apply=True, service_stopped=True)["applied"])
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_precommit_failure_rolls_back_all_steps_and_resumes(self):
        store = self.schema_one()
        before = self.rows("audit_events")
        for failing_target in ("4", SCHEMA_VERSION):
            def fail_step(con, target):
                if target == failing_target:
                    raise RuntimeError("fictional interruption before DDL step")
                _apply_step(con, target)
            with patch("v02.schema_upgrade._apply_step", side_effect=fail_step):
                with self.assertRaises(RuntimeError):
                    upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)
            self.assertEqual(inspect_schema(self.path), "1")
            self.assertEqual(self.rows("audit_events"), before)
            self.assertTrue(store.schema_upgrade_marker.exists())
            with self.assertRaises(ValueError):
                Storage(self.path)
        self.assertTrue(upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)["applied"])

    def test_anchor_failure_preserves_marker_and_resumes_without_new_history(self):
        store = self.prior_schema()
        with patch("checkpoint_security.CheckpointSecurity.write_anchor", side_effect=OSError("fictional anchor failure")):
            with self.assertRaises(OSError):
                upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), SCHEMA_VERSION)
        before, checkpoints = self.rows("audit_events"), self.rows("audit_checkpoints")
        with self.assertRaises(ValueError):
            Storage(self.path)
        with self.assertRaises(ValueError), store.connection():
            pass
        self.assertTrue(upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)["already_applied"])
        self.assertEqual(self.rows("audit_events"), before)
        self.assertEqual(self.rows("audit_checkpoints"), checkpoints)
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_schema_three_rejects_each_future_kind_even_when_audited(self):
        for kind in (*HISTORY_KINDS, LEASE_KIND):
            with self.subTest(kind=kind):
                self.path = self.folder / (kind + ".db")
                store = self.prior_schema()
                self.record(store, kind, "fictional-future-record")
                with store.connection() as con:
                    self.assertTrue(store.audit.verify(con)["valid"])
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "newer schema"):
                    upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(store.schema_upgrade_marker.exists())

    def test_history_is_immutable_while_lease_updates_require_current_version(self):
        store = Storage(self.path)
        for kind in HISTORY_KINDS:
            self.assertIn(kind, KNOWLEDGE_IMMUTABLE_KINDS)
            self.record(store, kind, kind)
            with self.assertRaises(Conflict):
                self.record(store, kind, kind, expected=1, active=False)
            with self.assertRaises(sqlite3.IntegrityError), store.connection(write=True) as con:
                con.execute("UPDATE records SET data_json='{}' WHERE id=?", (kind,))
        self.assertNotIn(LEASE_KIND, KNOWLEDGE_IMMUTABLE_KINDS)
        self.record(store, LEASE_KIND, "lease")
        lease = self.record(store, LEASE_KIND, "lease", expected=1, active=False)
        self.assertEqual(lease["version"], 2)
        with self.assertRaises(Conflict):
            self.record(store, LEASE_KIND, "lease", expected=1, active=True)
        self.assertTrue(Storage(self.path).verify()["valid"])
        with store.connection() as con:
            self.assertEqual(store.get(con, "fictional-owner", "lease"), lease)
            self.assertEqual(len(store.list(con, "fictional-owner", "knowledge_search_plan")), 1)

    def test_backup_restore_preserves_history_and_lease_versions(self):
        store = Storage(self.path)
        records = [self.record(store, kind, kind) for kind in (*HISTORY_KINDS, LEASE_KIND)]
        backup = store.backup("fictional-k3")
        self.record(store, LEASE_KIND, LEASE_KIND, expected=1, active=False)
        self.record(store, "knowledge_search_attempt", "later-attempt")
        self.assertTrue(restore(self.path, backup["filename"], apply=True, service_stopped=True)["verification"]["valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            for record in records:
                self.assertEqual(reopened.get(con, "fictional-owner", record["id"]), record)

    def test_restore_rejects_schema_three_before_checkpoint_or_marker(self):
        store = self.prior_schema()
        backup = store.backup("fictional-schema-three")
        upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            restore(self.path, backup["filename"], apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(store.maintenance_marker.exists())

    def test_principal_erasure_removes_history_and_leases_and_preserves_other_owner_exactly(self):
        store = Storage(self.path)
        survivors = []
        for kind in (*HISTORY_KINDS, LEASE_KIND):
            self.record(store, kind, "erase-" + kind)
            kept = self.record(store, kind, "keep-" + kind, principal="other-fictional-owner")
            if kind == LEASE_KIND:
                kept = self.record(store, kind, kept["id"], principal="other-fictional-owner", expected=1, active=False)
            survivors.append(kept)
        result = erase_principal(self.path, "fictional-owner", "fictional-erasure", apply=True,
                                 service_stopped=True, confirm_principal="fictional-owner")
        self.assertTrue(result["verification"]["valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM records WHERE principal_id='fictional-owner'").fetchone()[0], 0)
            for record in survivors:
                self.assertEqual(reopened.get(con, "other-fictional-owner", record["id"]), record)

    def test_schema_three_fts_tampering_blocks_upgrade_without_writes(self):
        store = self.prior_schema()
        with store.connection(write=True) as con:
            con.execute("INSERT INTO knowledge_fts(id,content) VALUES ('unowned','fictional forged index')")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-k3", apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(store.schema_upgrade_marker.exists())


if __name__ == "__main__":
    unittest.main()

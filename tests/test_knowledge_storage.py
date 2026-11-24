"""Fictional K1 storage fixtures only; no deployment DB or service is accessed."""
from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from v02.contracts import uid
from v02.maintenance import erase_principal, restore
from v02.schema_upgrade import upgrade
from v02.storage import Conflict, KNOWLEDGE_IMMUTABLE_KINDS, SCHEMA_VERSION, Storage, inspect_schema


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class KnowledgeStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = TEST_TMP / ("knowledge-storage-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.path = self.folder / "fictional.db"

    def tearDown(self):
        self.assertEqual(self.folder.resolve().parent, TEST_TMP.resolve())
        shutil.rmtree(self.folder)

    def put(self, store, identity="claim", principal="fictional-owner", text="The fictional beacon is blue.", expected=None):
        with store.connection(write=True) as con:
            return store.put(con, identity=identity, principal=principal, kind="knowledge_claim", conversation="fictional-chat",
                             data={"proposition": {"text": text}, "knowledge_state": "provisional"},
                             reason="fictional_fixture", expected=expected)

    def schema_one(self):
        """Construct the previous schema from its small synthetic canonical shape."""
        store = Storage.__new__(Storage)
        store._configure(self.path)
        schema = (ROOT / "v02/schema.sql").read_text(encoding="utf-8")
        schema = schema.replace("('schema_version','" + SCHEMA_VERSION + "')", "('schema_version','1')")
        schema = schema.replace("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(id UNINDEXED, content);", "")
        schema = schema[:schema.index("CREATE TRIGGER IF NOT EXISTS immutable_records")] + """
CREATE TRIGGER immutable_records BEFORE UPDATE OF data_json ON records
WHEN OLD.kind IN ('event','revision','run','observation','feedback','receipt','migration','memory_version','erasure')
BEGIN SELECT RAISE(ABORT,'immutable canonical record'); END;
"""
        with store.connection() as con:
            con.executescript(schema)
            store.security.migrate_schema(con)
            store.security.ensure_identity(con)
            store.put(con, identity="old-episode", principal="fictional-owner", kind="memory", conversation="fictional-chat",
                      data={"content": "A fictional episode.", "keywords": ["fixture"]}, reason="fictional_fixture")
        store.checkpoint("fictional_schema_one")
        return store

    def rows(self, table):
        con = sqlite3.connect(self.path)
        try:
            return con.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
        finally:
            con.close()

    def test_claim_updates_preserve_versions_and_indexes_transactionally(self):
        store = Storage(self.path)
        first = self.put(store)
        second = self.put(store, text="The fictional beacon is green.", expected=1)
        self.assertEqual(second["version"], 2)
        with store.connection() as con:
            snapshot = store.get(con, "fictional-owner", uid("knowledge_claim_version", "claim", 1))
            self.assertEqual(snapshot["data"], {"claim_id": "claim", "version": 1, "snapshot": first["data"]})
            self.assertEqual(con.execute("SELECT content FROM knowledge_fts WHERE id='claim'").fetchone()[0], second["data"]["proposition"]["text"])
        with self.assertRaises(Conflict):
            self.put(store, text="A stale statement.", expected=1)
        with store.connection() as con:
            self.assertEqual(store.get(con, "fictional-owner", "claim"), second)
            self.assertEqual(len(store.list(con, "fictional-owner", "knowledge_claim_version")), 1)
        self.assertTrue(Storage(self.path).verify()["knowledge_fts_valid"])

    def test_command_receipt_replays_without_reapplying_knowledge(self):
        store = Storage(self.path)
        def operation(con):
            return store.put(con, identity="claim", principal="fictional-owner", kind="knowledge_claim", conversation=None,
                             data={"proposition": {"text": "Fictional knowledge."}}, reason="fictional_fixture")
        first = store.command("fictional-owner", "knowledge.create", "fictional-key", {"text": "fixture"}, operation)
        again = store.command("fictional-owner", "knowledge.create", "fictional-key", {"text": "fixture"}, operation)
        self.assertEqual(first, again)
        with self.assertRaises(Conflict):
            store.command("fictional-owner", "knowledge.create", "fictional-key", {"text": "changed"}, operation)
        self.assertTrue(store.verify()["valid"])

    def test_knowledge_supporting_records_are_immutable_in_command_and_sql(self):
        store = Storage(self.path)
        for kind in KNOWLEDGE_IMMUTABLE_KINDS:
            with self.subTest(kind=kind):
                with store.connection(write=True) as con:
                    store.put(con, identity=kind, principal="fictional-owner", kind=kind, conversation=None,
                              data={"fictional": True}, reason="fictional_fixture")
                with self.assertRaises(Conflict), store.connection(write=True) as con:
                    store.put(con, identity=kind, principal="fictional-owner", kind=kind, conversation=None,
                              data={"fictional": False}, expected=1, reason="fictional_mutation")
                with self.assertRaises(sqlite3.IntegrityError), store.connection(write=True) as con:
                    con.execute("UPDATE records SET data_json='{}' WHERE id=?", (kind,))
        self.assertTrue(store.verify()["valid"])

    def test_knowledge_index_tampering_prevents_restart(self):
        store = Storage(self.path)
        self.put(store)
        with store.connection(write=True) as con:
            con.execute("INSERT INTO knowledge_fts(id,content) VALUES ('claim','Injected duplicate')")
        verification = store.verify()
        self.assertTrue(verification["audit"]["valid"])
        self.assertFalse(verification["knowledge_fts_valid"])
        with self.assertRaises(ValueError):
            Storage(self.path)

    def test_schema_one_startup_and_dry_run_do_not_write(self):
        store = self.schema_one()
        before, anchor = self.path.read_bytes(), store.security.anchor_path.read_bytes()
        with self.assertRaises(ValueError):
            Storage(self.path)
        plan = upgrade(self.path, "fictional-upgrade")
        self.assertFalse(plan["applied"])
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-upgrade", apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.security.anchor_path.read_bytes(), anchor)
        self.assertFalse(store.schema_upgrade_marker.exists())
        con = sqlite3.connect(self.path)
        try:
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'").fetchone())
        finally:
            con.close()

    def test_upgrade_preserves_audit_prefix_records_and_idempotency(self):
        self.schema_one()
        before_audit, before_records = self.rows("audit_events"), self.rows("records")
        result = upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)
        self.assertTrue(result["applied"])
        self.assertEqual(inspect_schema(self.path), SCHEMA_VERSION)
        self.assertEqual(self.rows("audit_events")[:len(before_audit)], before_audit)
        self.assertEqual(self.rows("records")[:len(before_records)], before_records)
        checkpoint_count = len(self.rows("audit_checkpoints"))
        self.assertTrue(upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)["already_applied"])
        self.assertEqual(len(self.rows("audit_checkpoints")), checkpoint_count)
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_upgrade_failure_before_commit_rolls_back_and_resumes(self):
        store = self.schema_one()
        before = self.rows("audit_events")
        with patch("v02.schema_upgrade.Storage.put", side_effect=RuntimeError("fictional interruption")):
            with self.assertRaises(RuntimeError):
                upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), "1")
        self.assertEqual(self.rows("audit_events"), before)
        self.assertTrue(store.schema_upgrade_marker.exists())
        with self.assertRaises(ValueError):
            Storage(self.path)
        with self.assertRaises(ValueError):
            upgrade(self.path, "wrong-operation", apply=True, service_stopped=True)
        self.assertTrue(upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)["applied"])

    def test_upgrade_anchor_failure_resumes_without_replaying_commit(self):
        store = self.schema_one()
        with patch("checkpoint_security.CheckpointSecurity.write_anchor", side_effect=OSError("fictional anchor interruption")):
            with self.assertRaises(OSError):
                upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), SCHEMA_VERSION)
        before = self.rows("audit_events")
        self.assertTrue(store.schema_upgrade_marker.exists())
        with self.assertRaises(ValueError):
            Storage(self.path)
        self.assertTrue(upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)["already_applied"])
        self.assertEqual(self.rows("audit_events"), before)
        self.assertFalse(store.schema_upgrade_marker.exists())
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_upgrade_rejects_invalid_source_audit_without_marking_or_writing(self):
        store = self.schema_one()
        with store.connection(write=True) as con:
            con.execute("UPDATE records SET data_json='{}' WHERE id='old-episode'")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(store.schema_upgrade_marker.exists())

    def test_restore_rejects_schema_one_before_checkpoint_or_maintenance(self):
        old_store = self.schema_one()
        backup = old_store.backup("fictional-old-schema")
        upgrade(self.path, "fictional-upgrade", apply=True, service_stopped=True)
        before = self.path.read_bytes()
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaises(ValueError):
                restore(self.path, backup["filename"], apply=apply, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(old_store.maintenance_marker.exists())

    def test_backup_restore_preserves_knowledge_history_and_fts(self):
        store = Storage(self.path)
        self.put(store)
        saved = self.put(store, text="Fictional beacon revised.", expected=1)
        backup = store.backup("fictional-knowledge")
        self.assertTrue(store.backups.verify(backup["filename"])["valid"])
        self.put(store, text="Later fictional revision.", expected=2)
        result = restore(self.path, backup["filename"], apply=True, service_stopped=True)
        self.assertTrue(result["verification"]["valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            self.assertEqual(reopened.get(con, "fictional-owner", "claim"), saved)
            self.assertEqual(len(reopened.list(con, "fictional-owner", "knowledge_claim_version")), 1)

    def test_erasure_removes_all_knowledge_kinds_and_preserves_other_owner_exactly(self):
        store = Storage(self.path)
        self.put(store)
        self.put(store, expected=1, text="A second fictional claim version.")
        survivor = self.put(store, identity="other-claim", principal="other-fictional-owner")
        survivor = self.put(store, identity="other-claim", principal="other-fictional-owner", expected=1, text="Other owner's revised fixture.")
        for kind in KNOWLEDGE_IMMUTABLE_KINDS - {"knowledge_claim_version", "schema_upgrade"}:
            with store.connection(write=True) as con:
                store.put(con, identity=kind, principal="fictional-owner", kind=kind, conversation=None,
                          data={"fictional": True}, reason="fictional_fixture")
        with store.connection() as con:
            survivor_history = store.list(con, "other-fictional-owner", "knowledge_claim_version")
        result = erase_principal(self.path, "fictional-owner", "fictional-erasure", apply=True,
                                 service_stopped=True, confirm_principal="fictional-owner")
        self.assertTrue(result["verification"]["valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM records WHERE principal_id='fictional-owner'").fetchone()[0], 0)
            self.assertEqual(reopened.get(con, "other-fictional-owner", "other-claim"), survivor)
            self.assertEqual(reopened.list(con, "other-fictional-owner", "knowledge_claim_version"), survivor_history)
            self.assertEqual([r[0] for r in con.execute("SELECT id FROM knowledge_fts")], ["other-claim"])


if __name__ == "__main__":
    unittest.main()

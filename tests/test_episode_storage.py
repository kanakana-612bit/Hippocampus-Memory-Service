"""Schema 5 storage tests using fictional records and isolated temporary DBs only."""
from __future__ import annotations

import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from v02.maintenance import erase_principal, restore
from v02.schema_upgrade import upgrade
from v02.storage import Conflict, EPISODE_IMMUTABLE_KINDS, Storage, inspect_schema


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class EpisodeStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = TEST_TMP / ("episode-storage-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.path = self.folder / "fictional.db"

    def tearDown(self):
        self.assertEqual(self.folder.resolve().parent, TEST_TMP.resolve())
        shutil.rmtree(self.folder)

    def event(self, store, identity="fictional-event", principal="fictional-owner"):
        with store.connection(write=True) as con:
            return store.put(con, identity=identity, principal=principal, kind="event",
                             conversation="fictional-chat", reason="fictional_fixture",
                             data={"content": "架空の灯台の記録。 Fictional lighthouse record.",
                                   "role": "user", "event_time": "2026-01-01T10:00:00+09:00",
                                   "provenance": {"source": "fictional_fixture"}})

    def historical(self, version="4"):
        """Build a genuinely old schema without any raw-text index."""
        store = Storage.__new__(Storage)
        store._configure(self.path)
        schema = (ROOT / "v02/schema.sql").read_text(encoding="utf-8")
        schema = schema.replace("('schema_version','5')", "('schema_version','" + version + "')")
        schema = schema.replace("CREATE VIRTUAL TABLE IF NOT EXISTS raw_event_fts USING fts5(id UNINDEXED, content);", "")
        schema = schema.replace(",\n                 'episode_chunk','episode_summary','episode','structured_candidate','structured_review','artifact_reference','episode_job'", "")
        if int(version) < 4:
            schema = schema.replace(",\n                 'knowledge_search_plan','knowledge_search_attempt','knowledge_search_observation'", "")
        if int(version) < 3:
            schema = schema.replace(",\n                 'knowledge_extraction','knowledge_migration'", "")
        if version == "1":
            schema = schema.replace("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(id UNINDEXED, content);", "")
        with store.connection() as con:
            con.executescript(schema)
            store.security.migrate_schema(con)
            store.security.ensure_identity(con)
        self.event(store)
        store.checkpoint("fictional_historical_schema")
        return store

    def rows(self, table):
        con = sqlite3.connect(self.path)
        try:
            return con.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
        finally:
            con.close()

    def test_new_events_are_indexed_without_changing_primary_text(self):
        store = Storage(self.path)
        before = self.event(store)
        with store.connection() as con:
            self.assertEqual(con.execute("SELECT id FROM raw_event_fts WHERE raw_event_fts MATCH ?",
                                         ('"lighthouse"',)).fetchone()[0], before["id"])
            self.assertEqual(con.execute("SELECT content FROM raw_event_fts WHERE id=?",
                                         (before["id"],)).fetchone()[0], before["data"]["content"])
        reopened = Storage(self.path)
        self.assertTrue(reopened.verify()["raw_event_fts_valid"])
        with reopened.connection() as con:
            self.assertEqual(reopened.get(con, "fictional-owner", before["id"]), before)

    def test_event_and_index_rollback_together(self):
        store = Storage(self.path)
        with self.assertRaises(RuntimeError), store.connection(write=True) as con:
            store.put(con, identity="rolled-back", principal="fictional-owner", kind="event",
                      conversation="fictional-chat", data={"content": "Fictional rollback."},
                      reason="fictional_fixture")
            raise RuntimeError("fictional transaction failure")
        with store.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM records WHERE kind='event'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM raw_event_fts").fetchone()[0], 0)
        self.assertTrue(store.verify()["valid"])

    def test_derived_history_is_immutable_but_job_state_accepts_versioned_updates(self):
        store = Storage(self.path)
        for kind in EPISODE_IMMUTABLE_KINDS:
            with self.subTest(kind=kind):
                with store.connection(write=True) as con:
                    store.put(con, identity=kind, principal="fictional-owner", kind=kind,
                              conversation="fictional-chat", data={"fictional": True}, reason="fictional_fixture")
                with self.assertRaises(Conflict), store.connection(write=True) as con:
                    store.put(con, identity=kind, principal="fictional-owner", kind=kind,
                              conversation="fictional-chat", data={"fictional": False},
                              expected=1, reason="fictional_mutation")
                with self.assertRaises(sqlite3.IntegrityError), store.connection(write=True) as con:
                    con.execute("UPDATE records SET data_json='{}' WHERE id=?", (kind,))
        with store.connection(write=True) as con:
            store.put(con, identity="job-state", principal="fictional-owner", kind="episode_job_state",
                      conversation="fictional-chat", data={"status": "active"}, reason="fictional_fixture")
            updated = store.put(con, identity="job-state", principal="fictional-owner", kind="episode_job_state",
                                conversation="fictional-chat", data={"status": "resolved"}, expected=1,
                                reason="fictional_fixture")
        self.assertEqual(updated["version"], 2)
        self.assertTrue(store.verify()["valid"])

    def test_corrupt_or_missing_raw_index_blocks_startup_without_repair(self):
        store = Storage(self.path)
        self.event(store)
        with store.connection(write=True) as con:
            con.execute("INSERT INTO raw_event_fts(id,content) VALUES ('fictional-event','tampered fixture')")
        self.assertFalse(store.verify()["raw_event_fts_valid"])
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            Storage(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with store.connection(write=True) as con:
            con.execute("DROP TABLE raw_event_fts")
        with self.assertRaises(ValueError):
            Storage(self.path)

    def test_schema_four_migration_preserves_records_and_audit_prefix(self):
        store = self.historical()
        records, audit = self.rows("records"), self.rows("audit_events")
        before = self.path.read_bytes()
        anchor = store.security.anchor_path.read_bytes()
        with self.assertRaises(ValueError):
            Storage(self.path)
        plan = upgrade(self.path, "fictional-4-to-5")
        self.assertEqual(plan["steps"], [{"from_schema": "4", "to_schema": "5"}])
        self.assertFalse(plan["applied"])
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-4-to-5", apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.security.anchor_path.read_bytes(), anchor)
        result = upgrade(self.path, "fictional-4-to-5", apply=True, service_stopped=True)
        self.assertTrue(result["verification"]["raw_event_fts_valid"])
        self.assertEqual(self.rows("records")[:len(records)], records)
        self.assertEqual(self.rows("audit_events")[:len(audit)], audit)
        self.assertTrue(Storage(self.path).verify()["valid"])
        self.assertTrue(upgrade(self.path, "fictional-4-to-5", apply=True, service_stopped=True)["already_applied"])

    def test_historical_targets_remain_explicit_and_do_not_install_future_index(self):
        self.historical("1")
        for target in ("2", "3", "4"):
            result = upgrade(self.path, "fictional-to-" + target, target_schema=target,
                             apply=True, service_stopped=True)
            self.assertEqual(result["current_schema"], target)
            con = sqlite3.connect(self.path)
            try:
                self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='raw_event_fts'").fetchone())
                trigger = con.execute("SELECT sql FROM sqlite_master WHERE name='immutable_records'").fetchone()[0]
                self.assertNotIn("episode_chunk", trigger)
            finally:
                con.close()
        upgrade(self.path, "fictional-to-5", apply=True, service_stopped=True)
        self.assertTrue(Storage(self.path).verify()["valid"])
        self.assertTrue(upgrade(self.path, "fictional-to-2", target_schema="2")["already_applied"])

    def test_index_backfill_rolls_back_and_resumes_with_upgrade_receipt(self):
        store = self.historical()
        records, audit = self.rows("records"), self.rows("audit_events")
        with patch("v02.schema_upgrade.Storage.put", side_effect=RuntimeError("fictional interruption")):
            with self.assertRaises(RuntimeError):
                upgrade(self.path, "fictional-retry", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), "4")
        self.assertEqual(self.rows("records"), records)
        self.assertEqual(self.rows("audit_events"), audit)
        self.assertTrue(store.schema_upgrade_marker.exists())
        con = sqlite3.connect(self.path)
        try:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='raw_event_fts'").fetchone())
        finally:
            con.close()
        self.assertTrue(upgrade(self.path, "fictional-retry", apply=True, service_stopped=True)["applied"])
        self.assertTrue(Storage(self.path).verify()["valid"])

    def test_schema_four_with_future_records_is_rejected_before_writes(self):
        store = self.historical()
        with store.connection(write=True) as con:
            store.put(con, identity="future-record", principal="fictional-owner", kind="episode_chunk",
                      conversation="fictional-chat", data={"fictional": True}, reason="fictional_fixture")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-invalid-source", apply=True, service_stopped=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(store.schema_upgrade_marker.exists())

    def test_missing_primary_text_rolls_back_backfill_before_schema_commit(self):
        store = self.historical()
        with store.connection(write=True) as con:
            store.put(con, identity="invalid-primary", principal="fictional-owner", kind="event",
                      conversation="fictional-chat", data={"fictional": True}, reason="fictional_fixture")
        records, audit = self.rows("records"), self.rows("audit_events")
        with self.assertRaises(ValueError):
            upgrade(self.path, "fictional-invalid-text", apply=True, service_stopped=True)
        self.assertEqual(inspect_schema(self.path), "4")
        self.assertEqual(self.rows("records"), records)
        self.assertEqual(self.rows("audit_events"), audit)
        self.assertTrue(store.schema_upgrade_marker.exists())

    def test_explicit_erasure_rebuilds_only_surviving_event_index(self):
        store = Storage(self.path)
        self.event(store)
        survivor = self.event(store, "other-event", "other-fictional-owner")
        with store.connection(write=True) as con:
            store.put(con, identity="derived", principal="fictional-owner", kind="episode_summary",
                      conversation="fictional-chat", data={"content": "Fictional secondary memory."},
                      reason="fictional_fixture")
        result = erase_principal(self.path, "fictional-owner", "fictional-erasure", apply=True,
                                 service_stopped=True, confirm_principal="fictional-owner")
        self.assertTrue(result["verification"]["raw_event_fts_valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            self.assertEqual([row[0] for row in con.execute("SELECT id FROM raw_event_fts")], ["other-event"])
            self.assertEqual(reopened.get(con, "other-fictional-owner", "other-event"), survivor)
            self.assertEqual(con.execute("SELECT count(*) FROM records WHERE principal_id='fictional-owner'").fetchone()[0], 0)

    def test_signed_backup_restore_preserves_raw_and_derived_history(self):
        store = Storage(self.path)
        primary = self.event(store)
        with store.connection(write=True) as con:
            derived = store.put(con, identity="summary", principal="fictional-owner", kind="episode_summary",
                                conversation="fictional-chat", data={"source_event_ids": [primary["id"]]},
                                reason="fictional_fixture")
        backup = store.backup("fictional-episodes")
        self.event(store, "later-event")
        result = restore(self.path, backup["filename"], apply=True, service_stopped=True)
        self.assertTrue(result["verification"]["raw_event_fts_valid"])
        reopened = Storage(self.path)
        with reopened.connection() as con:
            self.assertEqual(reopened.get(con, "fictional-owner", "summary"), derived)
            self.assertEqual(reopened.get(con, "fictional-owner", primary["id"]), primary)
            self.assertEqual(con.execute("SELECT count(*) FROM raw_event_fts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

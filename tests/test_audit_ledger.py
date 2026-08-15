from __future__ import annotations

import sqlite3
import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class AuditLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"hippocampus-audit-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def test_phase4_status_and_empty_chain_are_valid(self) -> None:
        status = self.manager.phase4_status()

        self.assertTrue(status["complete"])
        self.assertTrue(status["verification"]["valid"])
        self.assertEqual(status["verification"]["event_count"], 0)
        self.assertFalse(status["capabilities"]["signed_checkpoints"])

    def test_source_attribution_and_derivation_are_preserved(self) -> None:
        result = self.manager.ingest_messages(
            "audit-conversation",
            [
                {
                    "id": "audit-source-message",
                    "role": "user",
                    "content": "This is very important; keyword search is preferred.",
                    "actor_id": "user-public-example",
                    "actor_role": "user",
                    "source_channel": "test_client",
                    "content_origin": "original",
                }
            ],
        )
        raw = self.manager.recent_raw_messages("audit-conversation", limit=1)[0]
        trace = self.manager.get_memory_trace(result["memory_traces"][0])
        provenance = self.manager.get_provenance("memory_trace", trace["id"])

        self.assertEqual(raw["actor_id"], "user-public-example")
        self.assertEqual(raw["actor_role"], "user")
        self.assertEqual(raw["source_channel"], "test_client")
        self.assertEqual(trace["actor_role"], "system")
        self.assertEqual(trace["content_origin"], "summary")
        self.assertEqual(trace["extractor"], "deterministic_phase2_v2")
        self.assertTrue(
            any(
                edge["source_object_id"] == "audit-source-message"
                and edge["relation"] == "summarized_from"
                for edge in provenance["incoming"]
            )
        )
        self.assertTrue(self.manager.verify_audit()["valid"])

    def test_same_source_id_cannot_be_reassigned_or_rewritten(self) -> None:
        original = {
            "id": "stable-source-id",
            "role": "user",
            "content": "Original statement",
            "actor_id": "user-a",
        }
        self.manager.ingest_messages("conversation-a", [original], auto_capture=False)

        replay = self.manager.ingest_messages("conversation-a", [original], auto_capture=False)
        self.assertEqual(replay["raw_messages"], ["stable-source-id"])

        with self.assertRaisesRegex(ValueError, "already exists with different"):
            self.manager.ingest_messages(
                "conversation-a",
                [{**original, "content": "Rewritten statement"}],
                auto_capture=False,
            )
        with self.assertRaisesRegex(ValueError, "already exists with different"):
            self.manager.ingest_messages(
                "conversation-a",
                [{**original, "actor_id": "user-b"}],
                auto_capture=False,
            )

    def test_current_state_tampering_is_detected_independently_of_chain(self) -> None:
        self.manager.ingest_messages(
            "tamper-check",
            [{"id": "tamper-source", "role": "user", "content": "Original"}],
            auto_capture=False,
        )
        with self.manager.connect() as con:
            con.execute("UPDATE raw_messages SET content='Modified outside the service' WHERE id='tamper-source'")

        verification = self.manager.verify_audit()
        self.assertTrue(verification["chain_valid"])
        self.assertFalse(verification["current_state_valid"])
        self.assertFalse(verification["valid"])
        self.assertIn(
            "current_object_digest_mismatch",
            verification["object_errors"][0]["reasons"],
        )

    def test_audit_tables_reject_update_and_delete(self) -> None:
        self.manager.ingest_messages(
            "append-only",
            [{"id": "append-only-source", "role": "user", "content": "Recorded"}],
            auto_capture=False,
        )
        with self.manager.connect() as con:
            event_id = con.execute("SELECT event_id FROM audit_events LIMIT 1").fetchone()[0]
            payload_json = con.execute(
                "SELECT payload_json FROM audit_events WHERE event_id=?", (event_id,)
            ).fetchone()[0]
            self.assertNotIn("Recorded", payload_json)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                con.execute("UPDATE audit_events SET actor_role='other' WHERE event_id=?", (event_id,))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                con.execute("DELETE FROM audit_events WHERE event_id=?", (event_id,))

    def test_hash_chain_and_provenance_graph_tampering_are_detected(self) -> None:
        self.manager.ingest_messages(
            "chain-tamper",
            [{"id": "chain-source", "role": "user", "content": "Recorded"}],
            auto_capture=False,
        )
        with self.manager.connect() as con:
            event_id = con.execute("SELECT event_id FROM audit_events LIMIT 1").fetchone()[0]
            con.execute("DROP TRIGGER audit_events_no_update")
            con.execute("UPDATE audit_events SET actor_role='other' WHERE event_id=?", (event_id,))
        verification = self.manager.verify_audit()
        self.assertFalse(verification["chain_valid"])
        self.assertFalse(verification["valid"])

        second_db = TEST_TMP / f"hippocampus-audit-edge-{uuid.uuid4().hex}.db"
        try:
            second = MemoryManager(second_db)
            second.ingest_messages(
                "edge-tamper",
                [{"id": "edge-source", "role": "user", "content": "Recorded"}],
                auto_capture=False,
            )
            with second.connect() as con:
                event_id = con.execute("SELECT event_id FROM audit_events LIMIT 1").fetchone()[0]
                con.execute(
                    """
                    INSERT INTO provenance_edges (
                        edge_id, source_object_type, source_object_id,
                        target_object_type, target_object_id, relation,
                        audit_event_id, created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        "edge_unexpected",
                        "raw_message",
                        "invented-source",
                        "raw_message",
                        "edge-source",
                        "invented_relation",
                        event_id,
                        "2026-01-01T00:00:00Z",
                    ),
                )
            edge_verification = second.verify_audit()
            self.assertTrue(edge_verification["chain_valid"])
            self.assertFalse(edge_verification["provenance_valid"])
            self.assertFalse(edge_verification["valid"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{second_db}{suffix}")
                if path.exists():
                    path.unlink()

    def test_chain_remains_valid_across_restart_and_lifecycle_changes(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "A restart-stable audited memory",
                "candidate_memory_type": "semantic",
                "actor_role": "system",
                "source_channel": "test_client",
            }
        )
        memory = self.manager.consolidate_memory_trace(trace["id"])
        self.manager.retrieve("restart-stable audited memory", update_recall=True)
        restarted = MemoryManager(self.db_path)

        verification = restarted.verify_audit()
        self.assertTrue(verification["valid"])
        self.assertGreaterEqual(verification["event_count"], 4)
        provenance = restarted.get_provenance("memory", memory["id"])
        self.assertTrue(
            any(edge["source_object_id"] == trace["id"] for edge in provenance["incoming"])
        )


if __name__ == "__main__":
    unittest.main()

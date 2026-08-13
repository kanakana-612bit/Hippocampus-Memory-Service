from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class TemporalMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"hippocampus-phase3-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def test_ingest_separates_event_receive_persist_and_source_times(self) -> None:
        result = self.manager.ingest_messages(
            "time-conversation",
            [
                {
                    "id": "time-message-1",
                    "role": "user",
                    "content": "次に時刻設計を確認する必要がある。",
                    "event_time": "2026-08-01T09:30:00",
                    "source_time": "2026-08-01T09:30:00",
                    "timezone": "Asia/Tokyo",
                    "time_source": "historical_export",
                }
            ],
        )

        with self.manager.connect() as con:
            row = con.execute("SELECT * FROM raw_messages WHERE id='time-message-1'").fetchone()
        trace = self.manager.list_memory_traces(conversation_id="time-conversation")[0]

        self.assertEqual(row["event_time"], "2026-08-01T00:30:00.000+00:00")
        self.assertEqual(row["source_time"], "2026-08-01T00:30:00.000+00:00")
        self.assertEqual(row["timezone"], "Asia/Tokyo")
        self.assertEqual(row["time_source"], "historical_export")
        self.assertNotEqual(row["received_at"], row["event_time"])
        self.assertIsNotNone(row["persisted_at"])
        self.assertGreater(row["ingest_delay_seconds"], 0.0)
        self.assertEqual(trace["event_time"], row["event_time"])
        self.assertEqual(trace["received_at"], row["received_at"])
        self.assertEqual(result["temporal_ingest"]["duration_clock"], "monotonic")
        self.assertGreaterEqual(result["temporal_ingest"]["processing_elapsed_ms"], 0.0)

    def test_relative_future_expression_becomes_a_validity_window(self) -> None:
        result = self.manager.ingest_messages(
            "future-conversation",
            [
                {
                    "id": "future-message-1",
                    "role": "user",
                    "content": "明日、設計レビューをする必要がある。",
                    "event_time": "2026-08-12T10:00:00+09:00",
                    "timezone": "Asia/Tokyo",
                }
            ],
        )
        trace = self.manager.get_memory_trace(result["memory_traces"][0])

        self.assertEqual(trace["candidate_memory_type"], "prospective")
        self.assertEqual(trace["valid_from"], "2026-08-12T15:00:00.000+00:00")
        self.assertEqual(trace["valid_until"], "2026-08-13T15:00:00.000+00:00")
        self.assertEqual(trace["source"]["temporal_expression"], "明日")
        self.assertIn("temporal_expression", trace["extraction_reasons"])

    def test_temporal_context_reports_elapsed_time_and_conversation_gap(self) -> None:
        self.manager.ingest_messages(
            "gap-conversation",
            [
                {
                    "id": "gap-1",
                    "role": "user",
                    "content": "First event",
                    "event_time": "2026-08-10T00:00:00Z",
                },
                {
                    "id": "gap-2",
                    "role": "assistant",
                    "content": "Second event",
                    "event_time": "2026-08-12T00:00:00Z",
                },
            ],
            auto_capture=False,
        )

        temporal = self.manager.build_temporal_context(
            conversation_id="gap-conversation",
            timezone="Asia/Tokyo",
            as_of="2026-08-12T01:00:00Z",
        )

        self.assertEqual(temporal["event_count"], 2)
        self.assertEqual(temporal["elapsed_since_latest_seconds"], 3600.0)
        self.assertEqual(temporal["gap_before_latest_seconds"], 172800.0)
        self.assertEqual(temporal["current_time"], "2026-08-12T10:00:00.000+09:00")
        self.assertIn("Do not treat an imported historical event", temporal["text"])

    def test_as_of_retrieval_distinguishes_current_and_historical_memory(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "Quartzclock was the active scheduling policy.",
                "candidate_memory_type": "semantic",
                "keywords": ["quartzclock"],
                "event_time": "2026-08-01T00:00:00Z",
                "valid_from": "2026-08-01T00:00:00Z",
                "valid_until": "2026-08-10T00:00:00Z",
            }
        )
        memory = self.manager.consolidate_memory_trace(trace["id"])

        during = self.manager.retrieve(
            "quartzclock",
            temporal_scope="current",
            as_of="2026-08-05T00:00:00Z",
            update_recall=False,
        )
        after = self.manager.retrieve(
            "quartzclock",
            temporal_scope="current",
            as_of="2026-08-12T00:00:00Z",
            update_recall=False,
        )
        historical = self.manager.retrieve(
            "quartzclock",
            temporal_scope="historical",
            as_of="2026-08-12T00:00:00Z",
            update_recall=False,
        )

        self.assertIn(memory["id"], [item.memory["id"] for item in during])
        self.assertNotIn(memory["id"], [item.memory["id"] for item in after])
        self.assertIn(memory["id"], [item.memory["id"] for item in historical])
        self.assertEqual(historical[0].memory["temporal_status"], "expired")

    def test_supersession_preserves_history_and_prior_epistemic_status(self) -> None:
        old_trace = self.manager.create_memory_trace(
            {
                "content": "Coppercalendar policy uses the old schedule.",
                "candidate_memory_type": "semantic",
                "keywords": ["coppercalendar"],
                "event_time": "2026-08-01T00:00:00Z",
                "valid_from": "2026-08-01T00:00:00Z",
            }
        )
        new_trace = self.manager.create_memory_trace(
            {
                "content": "Coppercalendar policy uses the revised schedule.",
                "candidate_memory_type": "semantic",
                "keywords": ["coppercalendar"],
                "event_time": "2026-08-10T00:00:00Z",
            }
        )
        old_memory = self.manager.consolidate_memory_trace(old_trace["id"])
        new_memory = self.manager.consolidate_memory_trace(new_trace["id"])

        relation = self.manager.supersede_long_term_memory(
            old_memory["id"],
            new_memory["id"],
            effective_at="2026-08-10T00:00:00Z",
        )
        current = self.manager.retrieve(
            "coppercalendar",
            temporal_scope="current",
            as_of="2026-08-12T00:00:00Z",
            update_recall=False,
        )
        before_replacement = self.manager.retrieve(
            "coppercalendar",
            temporal_scope="current",
            as_of="2026-08-05T00:00:00Z",
            update_recall=False,
        )
        historical = self.manager.retrieve(
            "coppercalendar",
            temporal_scope="historical",
            as_of="2026-08-12T00:00:00Z",
            update_recall=False,
        )

        self.assertEqual(relation["superseded"]["superseded_by"], new_memory["id"])
        self.assertEqual(relation["superseded"]["epistemic_status"], old_memory["epistemic_status"])
        self.assertEqual([item.memory["id"] for item in current], [new_memory["id"]])
        self.assertEqual([item.memory["id"] for item in before_replacement], [old_memory["id"]])
        self.assertEqual([item.memory["id"] for item in historical], [old_memory["id"]])

    def test_context_includes_temporal_context_even_without_memories(self) -> None:
        context = self.manager.build_context(
            "unmatched-query",
            conversation_id="empty-conversation",
            timezone="Asia/Tokyo",
            as_of="2026-08-12T03:00:00Z",
        )

        self.assertIn("<temporal_context>", context["memory_context"])
        self.assertTrue(context["memory_context"].endswith("</memory_context>"))
        self.assertEqual(context["temporal_context"]["timezone"], "Asia/Tokyo")

    def test_invalid_timezone_and_validity_window_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.ingest_messages(
                "invalid-timezone",
                [{"role": "user", "content": "test", "timezone": "Mars/Olympus"}],
            )
        with self.assertRaises(ValueError):
            self.manager.create_memory_trace(
                {
                    "content": "Invalid temporal interval",
                    "valid_from": "2026-08-12T00:00:00Z",
                    "valid_until": "2026-08-11T00:00:00Z",
                }
            )

    def test_phase2_database_is_migrated_to_phase3(self) -> None:
        with self.manager.connect() as con:
            con.execute("DROP INDEX IF EXISTS idx_raw_messages_sequence")
            con.execute("DROP INDEX IF EXISTS idx_memory_traces_temporal")
            con.execute("DROP INDEX IF EXISTS idx_memories_temporal")
            con.execute("ALTER TABLE raw_messages DROP COLUMN event_time")
            con.execute("DELETE FROM schema_migrations WHERE version=4")

        restarted = MemoryManager(self.db_path)
        status = restarted.phase3_status()

        self.assertTrue(status["complete"])
        self.assertEqual(status["missing_indexes"], [])
        self.assertTrue(all(not values for values in status["missing_columns"].values()))

    def test_phase3_status_is_complete(self) -> None:
        status = self.manager.phase3_status()

        self.assertEqual(status["phase"], 3)
        self.assertTrue(status["complete"])
        self.assertTrue(all(status["capabilities"].values()))

    def test_restart_and_update_preserve_time_provenance(self) -> None:
        ingested = self.manager.ingest_messages(
            "provenance-time-conversation",
            [{"id": "provenance-time-1", "role": "user", "content": "neutral event"}],
            auto_capture=False,
        )
        self.assertEqual(ingested["raw_messages"], ["provenance-time-1"])
        with self.manager.connect() as con:
            before_raw = dict(
                con.execute("SELECT * FROM raw_messages WHERE id='provenance-time-1'").fetchone()
            )

        memory = self.manager.remember("Preserve the original persistence timestamp.")["memory"]
        before_memory = self.manager.get_long_term_memory(memory["id"])
        self.manager.patch_long_term_memory(memory["id"], {"content": "Updated wording."})
        after_memory = self.manager.get_long_term_memory(memory["id"])
        restarted = MemoryManager(self.db_path)
        with restarted.connect() as con:
            after_raw = dict(
                con.execute("SELECT * FROM raw_messages WHERE id='provenance-time-1'").fetchone()
            )

        self.assertEqual(after_memory["persisted_at"], before_memory["persisted_at"])
        self.assertEqual(after_raw["time_source"], before_raw["time_source"])
        self.assertEqual(after_raw["event_time"], before_raw["event_time"])


if __name__ == "__main__":
    unittest.main()

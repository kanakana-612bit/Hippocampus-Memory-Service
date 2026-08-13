from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class PublicImplementationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"public-status-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def test_short_and_long_term_layers_are_physically_separate(self) -> None:
        with self.manager.connect() as con:
            objects = {
                row[0]: row[1]
                for row in con.execute(
                    "SELECT name, type FROM sqlite_master WHERE name IN "
                    "('memory_traces', 'memories', 'long_term_memory_fts')"
                )
            }

        self.assertEqual(objects["memory_traces"], "table")
        self.assertEqual(objects["memories"], "table")
        self.assertEqual(objects["long_term_memory_fts"], "table")

    def test_phase1_state_survives_manager_restart(self) -> None:
        explicit = self.manager.remember(
            "A confirmed memory must survive a service restart.",
            keywords=["restart-confirmed"],
        )
        trace = self.manager.create_memory_trace(
            {
                "content": "A short-term trace must survive a service restart.",
                "candidate_memory_type": "episodic",
                "keywords": ["restart-trace"],
                "activation": 0.42,
                "epistemic_confidence": 0.63,
            }
        )
        recalled = self.manager.recall_memory_trace(trace["id"])

        restarted = MemoryManager(self.db_path)
        restored_explicit = restarted.get_long_term_memory(explicit["memory"]["id"])
        restored_trace = restarted.get_memory_trace(trace["id"])

        self.assertEqual(restored_explicit["epistemic_status"], "confirmed")
        self.assertEqual(restored_explicit["content"], explicit["memory"]["content"])
        self.assertEqual(restored_trace["recall_count"], recalled["recall_count"])
        self.assertEqual(restored_trace["activation"], recalled["activation"])
        self.assertEqual(restored_trace["epistemic_confidence"], 0.63)
        self.assertTrue(restarted.phase1_status()["complete"])

        explicit_results = restarted.retrieve("restart-confirmed", update_recall=False)
        self.assertIn(
            explicit["memory"]["id"],
            {result.memory["id"] for result in explicit_results},
        )

    def test_all_public_long_term_types_consolidate(self) -> None:
        for memory_type in ("episodic", "semantic", "prospective", "procedural"):
            with self.subTest(memory_type=memory_type):
                trace = self.manager.create_memory_trace(
                    {
                        "content": f"Public {memory_type} verification datum.",
                        "candidate_memory_type": memory_type,
                        "keywords": [f"verify_{memory_type}"],
                        "source_event_ids": [f"event-{memory_type}"],
                    }
                )
                memory = self.manager.consolidate_memory_trace(trace["id"])
                repeated = self.manager.consolidate_memory_trace(trace["id"])

                self.assertEqual(memory["memory_type"], memory_type)
                self.assertEqual(memory["source_event_ids"], [f"event-{memory_type}"])
                self.assertEqual(repeated["id"], memory["id"])

    def test_scores_recall_and_decay_are_independent_of_confidence(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "Scored public memory verification.",
                "candidate_memory_type": "semantic",
                "activation": 0.40,
                "salience": 0.70,
                "stability": 0.20,
                "epistemic_confidence": 0.56,
            }
        )
        self.assertGreater(trace["retention_score"], 0.0)
        recalled = self.manager.recall_memory_trace(trace["id"])

        self.assertGreater(recalled["activation"], trace["activation"])
        self.assertEqual(recalled["salience"], trace["salience"])
        self.assertEqual(recalled["epistemic_confidence"], trace["epistemic_confidence"])

        report = self.manager.maintain_memory_layers(as_of="2030-01-01T00:00:00+00:00")
        decayed = self.manager.get_memory_trace(trace["id"])
        self.assertGreaterEqual(report["traces_decayed"], 1)
        self.assertLess(decayed["activation"], recalled["activation"])
        self.assertEqual(decayed["epistemic_confidence"], recalled["epistemic_confidence"])

    def test_high_retention_trace_enters_review(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "High-retention review candidate.",
                "candidate_memory_type": "semantic",
                "activation": 1.0,
                "salience": 1.0,
                "stability": 1.0,
                "continuity_score": 1.0,
                "last_decayed_at": "2026-01-01T00:00:00+00:00",
            }
        )

        report = self.manager.maintain_memory_layers(as_of="2026-01-02T00:00:00+00:00")
        reviewed = self.manager.get_memory_trace(trace["id"])

        self.assertEqual(reviewed["status"], "review")
        self.assertEqual(report["traces_review"], 1)

    def test_review_can_keep_or_archive_a_trace(self) -> None:
        kept_trace = self.manager.create_memory_trace(
            {
                "content": "Keep this candidate for another review cycle.",
                "candidate_memory_type": "semantic",
            }
        )
        kept = self.manager.review_memory_trace(
            kept_trace["id"],
            decision="keep",
            notes="Retain without confirming.",
        )

        self.assertEqual(kept["trace"]["status"], "active")
        self.assertEqual(kept["trace"]["acquisition_mode"], "reviewed")
        self.assertEqual(kept["trace"]["epistemic_status"], "inferred")
        self.assertIsNone(kept["memory"])

        archived_trace = self.manager.create_memory_trace(
            {
                "content": "Archive this rejected candidate.",
                "candidate_memory_type": "semantic",
            }
        )
        archived = self.manager.review_memory_trace(
            archived_trace["id"],
            decision="archive",
        )

        self.assertEqual(archived["trace"]["status"], "archived")
        self.assertIsNone(archived["memory"])
        with self.manager.connect() as con:
            indexed = con.execute(
                "SELECT 1 FROM memory_trace_fts WHERE trace_id=?",
                (archived_trace["id"],),
            ).fetchone()
        self.assertIsNone(indexed)

    def test_fts_keyword_and_weighted_score_participate_in_retrieval(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "Quartzdelta is a deliberately unique retrieval token.",
                "candidate_memory_type": "semantic",
                "keywords": ["quartzdelta"],
                "activation": 0.73,
                "salience": 0.61,
                "stability": 0.44,
            }
        )
        memory = self.manager.consolidate_memory_trace(trace["id"])

        with self.manager.connect() as con:
            fts_hits = self.manager.layered.fts_hits(con, ["quartzdelta"])
        results = self.manager.retrieve("quartzdelta", update_recall=False)

        self.assertIn(memory["id"], fts_hits)
        result = next(result for result in results if result.memory["id"] == memory["id"])
        self.assertGreater(result.components["keyword_score"], 0.0)
        self.assertEqual(result.components["activation"], 0.73)
        self.assertEqual(result.components["salience"], 0.61)
        self.assertEqual(result.components["stability"], 0.44)
        self.assertGreater(result.relevance, 0.0)

    def test_compact_context_respects_budget_and_epistemic_labels(self) -> None:
        explicit = self.manager.remember(
            "Confirmed public context datum.",
            keywords=["public-context"],
        )
        trace = self.manager.create_memory_trace(
            {
                "content": "Tentative public context datum. " * 40,
                "candidate_memory_type": "semantic",
                "keywords": ["public-context"],
                "epistemic_status": "inferred",
            }
        )
        self.manager.consolidate_memory_trace(trace["id"])

        context = self.manager.build_context("public-context", char_budget=500)

        self.assertLessEqual(len(context["memory_context"]), 500)
        self.assertTrue(context["memory_context"].endswith("</memory_context>"))
        statuses = {
            item["memory"]["id"]: item["memory"]["epistemic_status"]
            for item in context["retrieved"]
        }
        self.assertEqual(statuses[explicit["memory"]["id"]], "confirmed")
        self.assertIn("inferred", statuses.values())


if __name__ == "__main__":
    unittest.main()

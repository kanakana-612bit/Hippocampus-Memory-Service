from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class LayeredMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"hippocampus-test-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def test_explicit_memory_is_confirmed_semantic_memory(self) -> None:
        result = self.manager.remember(
            "For technical support, preserve keyword-searchable evidence.",
            keywords=["technical", "keyword"],
        )

        canonical = self.manager.get_long_term_memory(result["memory"]["id"])

        self.assertEqual(canonical["memory_type"], "semantic")
        self.assertEqual(canonical["acquisition_mode"], "user_explicit")
        self.assertEqual(canonical["epistemic_status"], "confirmed")
        self.assertEqual(canonical["epistemic_confidence"], 1.0)
        self.assertTrue(canonical["pinned"])

        context = self.manager.build_context("keyword-searchable technical support")
        self.assertIn("keyword-searchable evidence", context["memory_context"])

    def test_trace_consolidates_with_evidence_link(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "conversation_id": "conversation-1",
                "turn_id": "turn-7",
                "trace_stage": "candidate",
                "candidate_memory_type": "episodic",
                "content": "The user appeared strongly interested in layered memory design.",
                "keywords": ["layered memory", "design"],
                "activation": 0.82,
                "salience": 0.91,
                "stability": 0.24,
                "epistemic_confidence": 0.68,
                "evidence_summary": "A strong positive response was observed in turn 7.",
                "source_event_ids": ["turn-7"],
            }
        )

        memory = self.manager.consolidate_memory_trace(trace["id"])
        updated_trace = self.manager.get_memory_trace(trace["id"])

        self.assertEqual(memory["memory_type"], "episodic")
        self.assertEqual(memory["epistemic_status"], "inferred")
        self.assertEqual(memory["epistemic_confidence"], 0.68)
        self.assertEqual(updated_trace["status"], "consolidated")
        self.assertEqual(updated_trace["consolidated_memory_id"], memory["id"])

        with self.manager.connect() as con:
            links = con.execute(
                "SELECT relation FROM memory_evidence_links WHERE memory_id=? ORDER BY relation",
                (memory["id"],),
            ).fetchall()
        self.assertEqual({row["relation"] for row in links}, {"consolidated_from", "supported_by"})

    def test_observation_fact_and_world_hypothesis_are_separate(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "Front range observation",
                "candidate_memory_type": "embodied",
                "epistemic_status": "observed",
                "observation_statement": "front_range sensor reported 1.20 m",
                "perspective": "sensor:front_range",
                "evidence_kind": "direct_measurement",
                "observation_fidelity": 1.0,
                "source_reliability": 0.86,
                "world_hypothesis": "An object may be approximately 1.2 m ahead",
                "epistemic_confidence": 0.78,
            }
        )

        self.assertEqual(trace["observation_statement"], "front_range sensor reported 1.20 m")
        self.assertEqual(trace["observation_fidelity"], 1.0)
        self.assertEqual(trace["world_hypothesis"], "An object may be approximately 1.2 m ahead")
        self.assertEqual(trace["epistemic_confidence"], 0.78)

    def test_recall_boosts_activation_without_changing_confidence(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "A keyword retrieval architecture was discussed.",
                "candidate_memory_type": "semantic",
                "keywords": ["keyword retrieval"],
                "activation": 0.40,
                "salience": 0.60,
                "stability": 0.20,
                "epistemic_confidence": 0.57,
            }
        )
        memory = self.manager.consolidate_memory_trace(trace["id"])

        retrieved = self.manager.retrieve("keyword retrieval", limit=1, update_recall=True)
        updated = self.manager.get_long_term_memory(memory["id"])

        self.assertEqual(len(retrieved), 1)
        self.assertGreater(updated["activation"], memory["activation"])
        self.assertEqual(updated["epistemic_confidence"], memory["epistemic_confidence"])
        self.assertEqual(updated["recall_count"], 1)

    def test_maintenance_archives_weak_trace(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "A weak transient observation.",
                "activation": 0.10,
                "salience": 0.0,
                "stability": 0.0,
                "continuity_score": 0.0,
                "last_decayed_at": "2026-01-01T00:00:00+00:00",
            }
        )

        report = self.manager.maintain_memory_layers(as_of="2026-01-10T00:00:00+00:00")
        updated = self.manager.get_memory_trace(trace["id"])

        self.assertEqual(updated["status"], "archived")
        self.assertEqual(report["traces_archived"], 1)

    def test_confirmed_memory_is_not_auto_archived(self) -> None:
        result = self.manager.remember("Keep this confirmed memory.", keywords=["confirmed"])
        memory_id = result["memory"]["id"]

        self.manager.patch_long_term_memory(
            memory_id,
            {"activation": 0.0, "salience": 0.0, "stability": 0.0},
        )
        self.manager.maintain_memory_layers(as_of="2030-01-01T00:00:00+00:00")
        updated = self.manager.get_long_term_memory(memory_id)

        self.assertFalse(updated["archived"])
        self.assertEqual(updated["epistemic_status"], "confirmed")

    def test_canonical_patch_updates_legacy_projection(self) -> None:
        result = self.manager.remember("Initial explicit content.", keywords=["initial"])
        memory_id = result["memory"]["id"]

        self.manager.patch_long_term_memory(memory_id, {"content": "Updated explicit content."})
        legacy = self.manager.get_memory("persistent", memory_id)

        self.assertEqual(legacy["content"], "Updated explicit content.")


if __name__ == "__main__":
    unittest.main()

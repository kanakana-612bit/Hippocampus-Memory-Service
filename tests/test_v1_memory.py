"""Fictional integration checks for authorization, read models and time revision."""
import json
from datetime import datetime, timedelta, timezone
from unittest import TestCase

import test_v1_learning as fixtures
from v1.common import Conflict, Denied, Invalid
from v1.contracts import Condition, EthicalWeights, TimeInterpretation
from v1.memory import Memory


class MemoryCase(TestCase):
    setUp = fixtures.V1Case.setUp
    tearDown = fixtures.V1Case.tearDown
    source = fixtures.V1Case.source
    run_learning = fixtures.V1Case.run_learning

    def populated(self):
        self.source()
        self.run_learning()
        self.memory = Memory(self.storage)
        rows = self.memory.timeline(self.owner)["items"]
        self.event = next(r for r in rows if r["kind"] == "event")
        self.episode = next(r for r in rows if r["kind"] == "episode")

    def expiry(self):
        return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    def test_all_history_pagination_japanese_and_unknown_time(self):
        self.populated()
        first = self.memory.timeline(self.owner, limit=1)
        second = self.memory.timeline(self.owner, limit=1, cursor=first["next_cursor"])
        third = self.memory.timeline(self.owner, limit=1, cursor=second["next_cursor"])
        self.assertEqual(len({p["items"][0]["id"] for p in (first, second, third)}), 3)
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(self.memory.timeline(self.owner, query="植物")["total"], 2)
        self.assertEqual(self.memory.timeline(self.owner, day="2026-09-24")["total"], 0)
        self.assertEqual(first["uncertain_time_counts"]["unknown"], 3)
        self.assertEqual(self.memory.timeline(self.owner.model_copy(update={"principal_id": "stranger"}))["total"], 0)

    def test_time_correction_preserves_original_and_invalidates_cursor(self):
        self.populated()
        cursor = self.memory.timeline(self.owner, limit=1)["next_cursor"]
        new_time = TimeInterpretation(time_quality="exact", event_time="2026-09-24T10:00:00+09:00", method="explicit_review")
        self.memory.correct_time(self.owner, self.event["id"], new_time, expected_version=1, reason="架空の時刻根拠確認", key="time")
        corrected = self.memory.get(self.owner, self.event["id"])
        self.assertEqual(corrected["data"], self.event["data"])
        self.assertEqual(corrected["time_head_version"], 2)
        self.assertEqual(len(self.memory.time_history(self.owner, self.event["id"])), 2)
        with self.assertRaises(Conflict):
            self.memory.timeline(self.owner, limit=1, cursor=cursor)
        with self.assertRaises(Conflict):
            self.memory.correct_time(self.owner, self.event["id"], new_time, expected_version=1, reason="古い版", key="stale")
        with self.assertRaises(Conflict):
            self.memory.correct_time(self.owner, self.event["id"], TimeInterpretation(time_quality="unknown"), expected_version=2, reason="自動降格不可", key="down")
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM reprocessing_queue").fetchone()[0], 1)

    def test_restriction_propagates_to_memories_contexts_counts_and_replays(self):
        self.populated()
        self.memory.set_ethics(self.owner, self.episode["id"], EthicalWeights(privacy_resistance=-1), reason="架空試験", key="ethics")
        with self.storage.connection() as con:
            source_version = con.execute("SELECT version FROM raw_sources").fetchone()[0]
            batch_id = con.execute("SELECT id FROM learning_batches").fetchone()[0]
        self.memory.set_policy(self.owner, target_kind="source", target_id="fiction-source", scope="restricted",
            destinations=["local"], expected_version=source_version, reason="架空の制限", key="policy")
        self.assertEqual(self.memory.timeline(self.owner)["total"], 0)
        self.assertEqual(self.memory.contexts(self.owner)["contexts"], [])
        self.assertEqual(self.memory.select(self.owner, "架空")["candidate_count"], 0)
        with self.assertRaises(Denied):
            self.learning.commit(self.owner, batch_id, key="commit-" + batch_id)
        with self.assertRaises(Denied):
            self.memory.set_ethics(self.owner, self.episode["id"], EthicalWeights(privacy_resistance=-1), reason="架空試験", key="ethics")

    def test_context_recall_without_lexical_gate_and_separate_confidence(self):
        self.populated()
        result = self.memory.select(self.owner, "架空の科学")
        self.assertGreater(result["candidate_count"], 0)
        self.assertEqual(result["selection"][0]["reasons"]["lexical"], 0)
        self.assertGreater(result["selection"][0]["reasons"]["context_participation"], 0)
        self.assertIsNone(self.memory.get(self.owner, self.episode["id"])["data"]["confidence"])

    def test_modifier_once_per_rule_and_surface_isolation(self):
        self.populated()
        context_id = self.memory.contexts(self.owner)["contexts"][0]["id"]
        situation = self.memory.set_situation(self.owner, room_name="架空の研究室", audience="owner", expires_at=self.expiry(), key="room")
        condition = Condition(op="any", children=(Condition(op="room", value=situation["room_id"]), Condition(op="audience", value="owner")))
        self.memory.set_modifier(self.owner, context_id, condition, modifier=.6, expires_at=self.expiry(), reason="架空の部屋補正", key="rule")
        result = self.memory.select(self.owner, "語句不一致")
        self.assertEqual(result["activation"][context_id]["value"], .6)
        self.assertEqual(len(result["activation"][context_id]["applied_rule_ids"]), 1)
        other = self.memory.select(self.owner.model_copy(update={"surface_id": "other"}), "語句不一致")
        self.assertEqual(other["activation"][context_id]["value"], 0)

    def test_guest_switch_invalidates_packets_learning_and_private_history(self):
        self.populated()
        selected = self.memory.prepare(self.owner, "架空", source_id="fiction-source", key="prepare")
        self.assertEqual(selected["selection"]["injection_status"], "not_injected")
        final = self.memory.finalize(self.owner, selected["run_id"], attempt=1, path_revision=1, key="final")
        self.assertEqual(final["provider_consumption"], "not_observed")
        self.assertTrue(final["packet"]["recent_raw"])
        self.memory.set_situation(self.owner, room_name="架空の公開室", audience="guest", expires_at=self.expiry(), key="guest")
        with self.assertRaises(Denied):
            self.memory.finalize(self.owner, selected["run_id"], attempt=1, path_revision=1, key="final")
        self.assertEqual(self.memory.timeline(self.owner)["total"], 0)
        with self.assertRaises(Denied):
            self.learning.create_run(self.owner, mode="daily", source_ids=["fiction-source"], key="guest-learn")

    def test_complete_turn_budget_never_truncates_original(self):
        self.populated()
        packet = self.memory.select(self.owner, "架空", source_id="fiction-source", recent_raw_budget=1)
        self.assertEqual(packet["recent_raw_ids"], [])
        self.assertEqual(packet["capacity"]["skipped_oversize_turns"], 1)
        self.assertEqual(self.memory.timeline(self.owner, kind="event")["total"], 2)

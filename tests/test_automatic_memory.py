from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class AutomaticMemoryEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"hippocampus-phase2-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def test_neutral_transient_message_is_not_encoded(self) -> None:
        result = self.manager.ingest_messages(
            "neutral-conversation",
            [{"id": "neutral-1", "role": "user", "content": "窓の外を見ました。"}],
        )

        self.assertEqual(result["memory_traces"], [])
        self.assertEqual(result["automatic_capture"]["skipped"], 1)
        self.assertEqual(self.manager.list_long_term_memories(), [])

    def test_phase2_signals_create_inferred_short_term_candidates(self) -> None:
        result = self.manager.ingest_messages(
            "signal-conversation",
            [
                {
                    "id": "emotion-1",
                    "role": "user",
                    "content": "本当に素晴らしい。完成した記憶機能に感動した！",
                },
                {
                    "id": "future-1",
                    "role": "user",
                    "content": "次に認証設計を検討する必要がある。",
                },
                {
                    "id": "preference-1",
                    "role": "user",
                    "content": "技術相談ではキーワード検索を優先する方針です。",
                },
                {
                    "id": "procedure-1",
                    "role": "user",
                    "content": "運用手順として、更新するときは先にテストする。",
                },
            ],
        )

        traces = self.manager.list_memory_traces(conversation_id="signal-conversation")
        by_turn = {trace["turn_id"]: trace for trace in traces}

        self.assertEqual(result["automatic_capture"]["created"], 4)
        self.assertEqual(by_turn["emotion-1"]["candidate_memory_type"], "episodic")
        self.assertGreater(by_turn["emotion-1"]["affect_signal"]["intensity"], 0.5)
        self.assertEqual(by_turn["future-1"]["candidate_memory_type"], "prospective")
        self.assertGreater(by_turn["future-1"]["unfinished_score"], 0.0)
        self.assertEqual(by_turn["preference-1"]["candidate_memory_type"], "semantic")
        self.assertEqual(by_turn["procedure-1"]["candidate_memory_type"], "procedural")
        self.assertTrue(all(trace["acquisition_mode"] == "automatic" for trace in traces))
        self.assertTrue(all(trace["epistemic_status"] == "inferred" for trace in traces))
        self.assertEqual(self.manager.list_long_term_memories(), [])

    def test_repetition_creates_then_reinforces_one_trace(self) -> None:
        content = "ログはUTF-8形式で保存する。"
        first = self.manager.ingest_messages(
            "repeat-conversation",
            [{"id": "repeat-1", "role": "user", "content": content}],
        )
        second = self.manager.ingest_messages(
            "repeat-conversation",
            [{"id": "repeat-2", "role": "user", "content": content}],
        )

        self.assertEqual(first["memory_traces"], [])
        self.assertEqual(second["automatic_capture"]["created"], 1)
        trace_id = second["memory_traces"][0]
        trace = self.manager.get_memory_trace(trace_id)
        self.assertIn("repetition", trace["extraction_reasons"])
        self.assertGreater(trace["repetition_score"], 0.0)
        self.assertEqual(trace["occurrence_count"], 1)

        replay = self.manager.ingest_messages(
            "repeat-conversation",
            [{"id": "repeat-2", "role": "user", "content": content}],
        )
        after_replay = self.manager.get_memory_trace(trace_id)
        self.assertEqual(replay["automatic_capture"]["created"], 0)
        self.assertEqual(replay["automatic_capture"]["reinforced"], 0)
        self.assertEqual(after_replay["occurrence_count"], 1)

        third = self.manager.ingest_messages(
            "repeat-conversation",
            [{"id": "repeat-3", "role": "user", "content": content}],
        )
        reinforced = self.manager.get_memory_trace(trace_id)
        self.assertEqual(third["automatic_capture"]["reinforced"], 1)
        self.assertEqual(len(self.manager.list_memory_traces(conversation_id="repeat-conversation")), 1)
        self.assertEqual(reinforced["occurrence_count"], 2)
        self.assertEqual(set(reinforced["source_event_ids"]), {"repeat-2", "repeat-3"})

    def test_explicit_instruction_uses_confirmed_route_only(self) -> None:
        payload = [
            {
                "id": "explicit-1",
                "role": "user",
                "content": "覚えておいて：テストには公開データだけを使う。",
            }
        ]
        first = self.manager.ingest_messages("explicit-conversation", payload)
        second = self.manager.ingest_messages("explicit-conversation", payload)

        self.assertEqual(first["memory_traces"], [])
        self.assertEqual(second["memory_traces"], [])
        self.assertEqual(first["persistent_memories"], second["persistent_memories"])
        memories = self.manager.list_long_term_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["acquisition_mode"], "user_explicit")
        self.assertEqual(memories[0]["epistemic_status"], "confirmed")

    def test_user_confirmation_remains_inferred_and_keeps_both_sources(self) -> None:
        result = self.manager.ingest_messages(
            "confirmation-conversation",
            [
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "公開版では自動抽出した内容を推定形で保持します。",
                },
                {
                    "id": "user-1",
                    "role": "user",
                    "content": "その通りです。これでよいです。",
                },
            ],
        )

        trace = self.manager.get_memory_trace(result["memory_traces"][0])
        self.assertEqual(trace["candidate_memory_type"], "semantic")
        self.assertEqual(trace["epistemic_status"], "inferred")
        self.assertGreater(trace["confirmation_score"], 0.0)
        self.assertEqual(trace["source_event_ids"], ["assistant-1", "user-1"])
        self.assertIn("直前のアシスタント発言", trace["content"])
        self.assertEqual(self.manager.list_long_term_memories(), [])

    def test_phase2_status_reports_complete_schema(self) -> None:
        status = self.manager.phase2_status()

        self.assertEqual(status["phase"], 2)
        self.assertTrue(status["complete"])
        self.assertEqual(status["missing_columns"], [])
        self.assertTrue(all(status["capabilities"].values()))

    def test_phase1_database_is_migrated_before_phase2_index_creation(self) -> None:
        with self.manager.connect() as con:
            con.execute("DROP INDEX IF EXISTS idx_memory_traces_fingerprint")
            con.execute("ALTER TABLE memory_traces DROP COLUMN capture_score")
            con.execute("DELETE FROM schema_migrations WHERE version=3")

        restarted = MemoryManager(self.db_path)
        status = restarted.phase2_status()

        self.assertTrue(status["complete"])
        self.assertEqual(status["missing_columns"], [])


if __name__ == "__main__":
    unittest.main()

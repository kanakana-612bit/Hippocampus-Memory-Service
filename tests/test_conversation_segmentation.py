from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from conversation_segmentation import (
    deterministic_session_boundaries,
    explicit_topic_candidates,
    token_chunks,
)
from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


def event(
    event_id: str,
    role: str,
    content: str,
    event_time: str,
    sequence: int,
    conversation_id: str = "segmentation-chat",
) -> dict[str, object]:
    return {
        "id": event_id,
        "conversation_id": conversation_id,
        "role": role,
        "actor_role": role,
        "content": content,
        "event_time": event_time,
        "created_at": event_time,
        "event_sequence": sequence,
    }


class ConversationSegmentationUnitTests(unittest.TestCase):
    def test_user_activity_gap_creates_session_boundary(self) -> None:
        events = [
            event("u1", "user", "First topic", "2026-08-16T09:00:00+00:00", 1),
            event("a1", "assistant", "Response", "2026-08-16T09:01:00+00:00", 2),
            event("u2", "user", "Later topic", "2026-08-16T11:00:00+00:00", 3),
        ]

        boundaries = deterministic_session_boundaries(events, gap_minutes=90)

        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0]["before_event_id"], "a1")
        self.assertEqual(boundaries[0]["after_event_id"], "u2")
        self.assertEqual(boundaries[0]["detection_source"], "deterministic")

    def test_transition_word_is_only_a_topic_candidate(self) -> None:
        events = [
            event("u1", "user", "Current topic", "2026-08-16T09:00:00+00:00", 1),
            event("a1", "assistant", "Response", "2026-08-16T09:01:00+00:00", 2),
            event("u2", "user", "ところで、別の設計を考えたい。", "2026-08-16T09:02:00+00:00", 3),
        ]

        candidates = explicit_topic_candidates(events)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["after_event_id"], "u2")
        self.assertEqual(candidates[0]["detection_source"], "rule_candidate")

    def test_token_chunks_keep_primary_membership_unique_with_overlap(self) -> None:
        events = [
            event(f"u{index}", "user", "内容" * 80, f"2026-08-16T09:0{index}:00+00:00", index)
            for index in range(1, 5)
        ]

        chunks = token_chunks(events, max_tokens=600, overlap_turns=1)

        primary_ids = [event_id for chunk in chunks for event_id in chunk["primary_event_ids"]]
        self.assertEqual(primary_ids, ["u1", "u2", "u3", "u4"])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk["estimated_tokens"] <= 600 for chunk in chunks))
        self.assertTrue(
            any(len(chunk["context_event_ids"]) > len(chunk["primary_event_ids"]) for chunk in chunks)
        )


class ConversationSegmentationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"segmentation-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)
        self.security_dir = self.manager.layered.security.security_dir

    def tearDown(self) -> None:
        for path in TEST_TMP.glob(f"{self.db_path.stem}*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        shutil.rmtree(self.security_dir, ignore_errors=True)

    def test_slm_topic_boundary_is_validated_and_persisted(self) -> None:
        events = [
            event("u1", "user", "Memory design", "2026-08-16T09:00:00+00:00", 1),
            event("a1", "assistant", "Response", "2026-08-16T09:01:00+00:00", 2),
            event("u2", "user", "ところで、音声設計を考える。", "2026-08-16T09:02:00+00:00", 3),
        ]
        response = json.dumps(
            {
                "boundaries": [
                    {
                        "after_event_id": "u2",
                        "confidence": 0.91,
                        "reason": "topic changed",
                        "signals": ["semantic_shift"],
                        "previous_topic": "memory design",
                        "next_topic": "speech design",
                    }
                ]
            }
        )

        with patch.object(self.manager, "chat_completion", return_value=response):
            analysis = self.manager.analyze_conversation_events(events)

        topic_boundaries = [
            item for item in analysis["boundaries"] if item["boundary_type"] == "topic"
        ]
        self.assertEqual(len(topic_boundaries), 1)
        self.assertEqual(topic_boundaries[0]["after_event_id"], "u2")
        self.assertEqual(topic_boundaries[0]["detection_source"], "slm")
        self.assertEqual(
            len([item for item in analysis["segments"] if item["segment_type"] == "topic"]),
            2,
        )
        stored = self.manager.list_conversation_boundaries(
            conversation_id="segmentation-chat"
        )
        self.assertEqual(stored[0]["after_event_id"], "u2")

    def test_slm_cannot_introduce_unknown_boundary_event(self) -> None:
        events = [
            event("u1", "user", "First", "2026-08-16T09:00:00+00:00", 1),
            event("u2", "user", "Second", "2026-08-16T09:02:00+00:00", 2),
        ]
        response = json.dumps(
            {
                "boundaries": [
                    {
                        "after_event_id": "invented-event",
                        "confidence": 0.99,
                        "reason": "invented",
                        "signals": [],
                        "previous_topic": None,
                        "next_topic": None,
                    }
                ]
            }
        )

        with patch.object(self.manager, "chat_completion", return_value=response):
            analysis = self.manager.analyze_conversation_events(events, persist=False)

        self.assertEqual(analysis["boundaries"], [])

    def test_nightly_extraction_uses_assistant_context_but_user_evidence(self) -> None:
        self.manager.ingest_messages(
            "nightly-context-chat",
            [
                {
                    "id": "user-source",
                    "role": "user",
                    "content": "この調整方法を次回も使いたい。",
                    "event_time": "2026-08-16T09:00:00+00:00",
                },
                {
                    "id": "assistant-context",
                    "role": "assistant",
                    "content": "調整方法を手順として整理します。",
                    "event_time": "2026-08-16T09:01:00+00:00",
                },
            ],
            auto_capture=False,
        )
        response = json.dumps(
            {
                "candidates": [
                    {
                        "source_event_ids": ["user-source", "assistant-context"],
                        "memory_type": "procedural",
                        "summary": "ユーザーはこの調整方法を次回も使いたいと考えている可能性がある。",
                        "keywords": ["調整方法"],
                        "confidence": 0.7,
                        "reason": "reusable procedure",
                    }
                ]
            },
            ensure_ascii=False,
        )
        captured_prompts: list[str] = []

        def completion(**kwargs: object) -> str:
            messages = kwargs["messages"]
            captured_prompts.append(str(messages[-1]["content"]))
            return response

        with patch.object(self.manager, "chat_completion", side_effect=completion):
            result = self.manager.run_nightly_extraction(
                conversation_id="nightly-context-chat",
                use_boundary_slm=False,
            )

        self.assertEqual(result["created"], 1)
        self.assertIn("actor=assistant", captured_prompts[0])
        trace = self.manager.get_memory_trace(result["trace_ids"][0])
        self.assertEqual(trace["source_event_ids"], ["user-source"])
        self.assertEqual(trace["extractor"], "slm_nightly_v2")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class AttributionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"attribution-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)
        self.security_dir = self.manager.layered.security.security_dir
        self.manager.ingest_messages(
            "attribution-public-example",
            [
                {
                    "id": "event-user-b",
                    "role": "user",
                    "content": "A can be understood as interpretation B.",
                    "actor_role": "user",
                    "content_origin": "original",
                },
                {
                    "id": "event-assistant-c",
                    "role": "assistant",
                    "content": "A also has the separate interpretive aspect C.",
                    "actor_role": "assistant",
                    "content_origin": "original",
                },
            ],
            auto_capture=False,
        )

    def tearDown(self) -> None:
        for path in TEST_TMP.glob(f"{self.db_path.stem}*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        shutil.rmtree(self.security_dir, ignore_errors=True)

    def test_agent_statement_misattributed_to_user_is_rejected(self) -> None:
        result = self.manager.validate_response_attribution(
            content='君が以前話していた「A also has the separate interpretive aspect C.」は正しい。',
            conversation_id="attribution-public-example",
        )

        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["claim_counts"]["contradicted"], 1)
        claim = result["claims"][0]
        self.assertEqual(claim["claimed_actor_role"], "user")
        self.assertEqual(claim["best_evidence"]["actor_role"], "assistant")
        self.assertEqual(claim["reason"], "best_evidence_actor_mismatch")

    def test_correct_actor_and_explicit_event_marker_are_verified(self) -> None:
        result = self.manager.validate_response_attribution(
            content=(
                '私が以前述べた「A also has the separate interpretive aspect C.」'
                "[[event:event-assistant-c]]を再検討します。"
            ),
            conversation_id="attribution-public-example",
        )

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["claim_counts"]["verified"], 1)
        self.assertNotIn("[[event:", result["safe_content"])
        self.assertEqual(
            result["claims"][0]["matching_evidence"]["reference_id"],
            "event-assistant-c",
        )

    def test_missing_evidence_is_unverified_not_false(self) -> None:
        result = self.manager.validate_response_attribution(
            content='あなたは以前「A requires interpretation D.」と言った。',
            conversation_id="attribution-public-example",
        )

        self.assertEqual(result["decision"], "unverified")
        self.assertEqual(result["claim_counts"]["unverified"], 1)

    def test_derived_record_cannot_support_direct_speech(self) -> None:
        self.manager.ingest_messages(
            "derived-example",
            [
                {
                    "id": "event-derived-user-summary",
                    "role": "user",
                    "content": "The user requires option Z.",
                    "actor_role": "user",
                    "content_origin": "summary",
                }
            ],
            auto_capture=False,
        )
        result = self.manager.validate_response_attribution(
            content=(
                'あなたが以前述べた「The user requires option Z.」'
                "[[event:event-derived-user-summary]]を採用します。"
            ),
            event_ids=["event-derived-user-summary"],
        )

        self.assertEqual(result["decision"], "reject")
        self.assertEqual(
            result["claims"][0]["reason"],
            "derived_evidence_cannot_support_direct_attribution",
        )

    def test_confirmed_explicit_memory_supports_a_request_but_not_a_quote(self) -> None:
        remembered = self.manager.remember(
            "Prefer keyword-searchable evidence.",
            keywords=["keyword-searchable"],
            dedupe=False,
        )["memory"]
        request = self.manager.validate_response_attribution(
            content=(
                "You previously requested keyword-searchable evidence."
                f"[[memory:{remembered['id']}]]"
            ),
            memory_ids=[remembered["id"]],
        )
        quote = self.manager.validate_response_attribution(
            content=(
                'You previously said "Prefer keyword-searchable evidence."'
                f"[[memory:{remembered['id']}]]"
            ),
            memory_ids=[remembered["id"]],
        )

        self.assertEqual(request["decision"], "allow")
        self.assertEqual(quote["decision"], "reject")

    def test_candidate_selector_prefers_verified_attribution(self) -> None:
        selected = self.manager.select_response_candidate(
            candidates=[
                {
                    "candidate_id": "wrong",
                    "content": 'You previously said "A also has the separate interpretive aspect C."',
                    "quality_score": 0.9,
                },
                {
                    "candidate_id": "right",
                    "content": (
                        'I previously said "A also has the separate interpretive aspect C."'
                        "[[event:event-assistant-c]]"
                    ),
                    "quality_score": 0.6,
                },
            ],
            conversation_id="attribution-public-example",
        )

        self.assertEqual(selected["decision"], "selected")
        self.assertEqual(selected["selected_candidate_id"], "right")
        self.assertNotIn("[[event:", selected["selected_content"])

    def test_non_attribution_answer_is_out_of_scope_and_allowed(self) -> None:
        result = self.manager.validate_response_attribution(
            content="A lightweight rule can check only speaker attribution.",
            conversation_id="attribution-public-example",
        )

        self.assertEqual(result["decision"], "allow")
        self.assertFalse(result["applicable"])

    def test_context_exposes_compact_actor_bound_references(self) -> None:
        trace = self.manager.create_memory_trace(
            {
                "content": "A also has the separate interpretive aspect C.",
                "candidate_memory_type": "episodic",
                "source_event_ids": ["event-assistant-c"],
                "derived_from": [
                    {
                        "object_type": "raw_message",
                        "object_id": "event-assistant-c",
                        "relation": "summarized_from",
                    }
                ],
            }
        )
        self.manager.consolidate_memory_trace(trace["id"])
        context = self.manager.build_context("interpretive aspect C", char_budget=3500)

        self.assertIn("event:event-assistant-c", context["memory_context"])
        self.assertIn("actor=assistant", context["memory_context"])
        self.assertNotIn("@assistant", context["memory_context"])
        self.assertIn("event-assistant-c", context["attribution_evidence"]["event_ids"])


if __name__ == "__main__":
    unittest.main()

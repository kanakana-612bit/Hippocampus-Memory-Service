from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class HardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"hardening-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)
        self.security_dir = self.manager.layered.security.security_dir

    def tearDown(self) -> None:
        for path in TEST_TMP.glob(f"{self.db_path.stem}*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        shutil.rmtree(self.security_dir, ignore_errors=True)

    def test_recipe_expression_becomes_a_short_term_candidate(self) -> None:
        result = self.manager.ingest_messages(
            "recipe-evaluation",
            [
                {
                    "id": "recipe-1",
                    "role": "user",
                    "content": (
                        "そこにレモンを絞って少し入れるアレンジを加えたら、美味しかったんだ。"
                        "新しい派生レシピとして残そうかと思って。"
                    ),
                }
            ],
        )

        self.assertEqual(result["automatic_capture"]["created"], 1)
        trace = self.manager.get_memory_trace(result["memory_traces"][0])
        self.assertEqual(trace["candidate_memory_type"], "procedural")
        self.assertIn("recipe-1", trace["source_event_ids"])
        self.assertEqual(trace["epistemic_status"], "inferred")

    def test_ingest_idempotency_replays_without_duplicate_work(self) -> None:
        payload = [
            {"id": "idempotent-1", "role": "user", "content": "この新しいレシピを残そう。"}
        ]
        first = self.manager.ingest_messages(
            "idempotent-chat", payload, idempotency_key="request-1"
        )
        replay = self.manager.ingest_messages(
            "idempotent-chat", payload, idempotency_key="request-1"
        )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["memory_traces"], replay["memory_traces"])
        self.assertEqual(len(self.manager.list_memory_traces(conversation_id="idempotent-chat")), 1)
        with self.manager.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM ingest_requests").fetchone()[0], 1)

    def test_reinforcement_adds_provenance_for_every_source(self) -> None:
        content = "美味しい新しいレシピとして残そう。"
        first = self.manager.ingest_messages(
            "provenance-reinforcement",
            [{"id": "source-a", "role": "user", "content": content}],
        )
        second = self.manager.ingest_messages(
            "provenance-reinforcement",
            [{"id": "source-b", "role": "user", "content": content}],
        )
        trace_id = first["memory_traces"][0]

        self.assertEqual(second["memory_traces"], [trace_id])
        trace = self.manager.get_memory_trace(trace_id)
        self.assertEqual(set(trace["source_event_ids"]), {"source-a", "source-b"})
        self.assertEqual(
            {item["object_id"] for item in trace["derived_from"]},
            {"source-a", "source-b"},
        )
        provenance = self.manager.get_provenance("memory_trace", trace_id)
        self.assertTrue(
            {"source-a", "source-b"}.issubset(
                {edge["source_object_id"] for edge in provenance["incoming"]}
            )
        )

    def test_marker_with_actor_suffix_is_parsed_and_removed(self) -> None:
        self.manager.ingest_messages(
            "marker-chat",
            [{"id": "marker-event", "role": "assistant", "content": "Option C was proposed."}],
            auto_capture=False,
        )
        result = self.manager.validate_response_attribution(
            content="I previously proposed Option C.[[event:marker-event@unknown]]",
            event_ids=["marker-event"],
        )

        self.assertEqual(result["decision"], "allow")
        self.assertNotIn("@unknown", result["safe_content"])
        self.assertNotIn("[[event:", result["safe_content"])

    def test_nightly_slm_extracts_structure_but_provenance_stays_deterministic(self) -> None:
        self.manager.ingest_messages(
            "nightly-chat",
            [{"id": "nightly-source", "role": "user", "content": "炭酸は少し弱めにした。"}],
            auto_capture=False,
        )
        response = json.dumps(
            {
                "candidates": [
                    {
                        "source_event_ids": ["nightly-source", "not-supplied"],
                        "memory_type": "procedural",
                        "summary": "ユーザーは炭酸を少し弱めにする調整を試した可能性がある。",
                        "keywords": ["炭酸", "調整"],
                        "confidence": 0.64,
                        "reason": "reusable adjustment",
                    }
                ]
            },
            ensure_ascii=False,
        )
        with patch.object(self.manager, "chat_completion", return_value=response):
            result = self.manager.run_nightly_extraction(
                conversation_id="nightly-chat", limit=10
            )

        self.assertEqual(result["created"], 1)
        trace = self.manager.get_memory_trace(result["trace_ids"][0])
        self.assertEqual(trace["source_event_ids"], ["nightly-source"])
        self.assertEqual(trace["extractor"], "slm_nightly_v2")
        self.assertEqual(trace["epistemic_status"], "inferred")

    def test_slm_claim_extraction_does_not_decide_actor_validity(self) -> None:
        self.manager.ingest_messages(
            "claim-chat",
            [{"id": "assistant-origin", "role": "assistant", "content": "AにはCの側面もある。"}],
            auto_capture=False,
        )
        structured = json.dumps(
            {
                "claims": [
                    {
                        "claimed_actor_role": "user",
                        "claim_kind": "speech",
                        "statement": "AにはCの側面もある",
                    }
                ]
            },
            ensure_ascii=False,
        )
        with patch.dict(
            os.environ,
            {"HIPPOCAMPUS_CLAIM_SLM_ENABLED": "1", "HIPPOCAMPUS_SLM_PROVIDER": "openai"},
        ), patch.object(
            self.manager, "chat_completion", return_value=structured
        ) as completion:
            result = self.manager.validate_response_attribution(
                content="君の言葉だったはずの『AにはCの側面もある』を再検討する。",
                conversation_id="claim-chat",
            )

        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["claims"][0]["detection"], "slm_structure_v1")
        self.assertEqual(result["claims"][0]["best_evidence"]["actor_role"], "assistant")
        self.assertEqual(
            completion.call_args.kwargs["response_format"]["type"],
            "json_schema",
        )

    def test_structured_claim_extraction_only_accepts_supplied_evidence_markers(self) -> None:
        structured = json.dumps(
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "said",
                        "content": "Aについて話した",
                        "evidence_marker": "event:invented-source",
                    }
                ]
            },
            ensure_ascii=False,
        )
        completion = {
            "content": structured,
            "provider": "ollama",
            "model": "test-model",
            "duration_ms": 10.0,
        }
        with patch.object(self.manager, "_structured_slm_completion", return_value=completion):
            result = self.manager.extract_structured_claims(
                content="ユーザーは以前Aについて話した。",
                source_role="assistant",
            )

        self.assertEqual(result["claims"][0]["evidence_marker"], None)
        self.assertEqual(result["gate_claims"][0]["event_ids"], [])

    def test_structured_claim_marker_is_converted_for_deterministic_gate(self) -> None:
        self.manager.ingest_messages(
            "structured-claim-chat",
            [{"id": "source-user-1", "role": "user", "content": "Aについて話した。"}],
            auto_capture=False,
        )
        structured = json.dumps(
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "said",
                        "content": "Aについて話した",
                        "evidence_marker": "event:source-user-1",
                    }
                ]
            },
            ensure_ascii=False,
        )
        completion = {
            "content": structured,
            "provider": "ollama",
            "model": "test-model",
            "duration_ms": 10.0,
        }
        with patch.object(self.manager, "_structured_slm_completion", return_value=completion):
            result = self.manager.extract_structured_claims(
                content="前にユーザーがAについて話した。",
                source_role="assistant",
                event_ids=["source-user-1"],
            )
        validation = self.manager.validate_response_attribution(
            content="前にユーザーがAについて話した。",
            conversation_id="structured-claim-chat",
            event_ids=["source-user-1"],
            claims=result["gate_claims"],
        )

        self.assertEqual(result["claims"][0]["evidence_marker"], "event:source-user-1")
        self.assertEqual(result["gate_claims"][0]["event_ids"], ["source-user-1"])
        self.assertEqual(validation["decision"], "allow")

    def test_clear_second_person_and_quote_are_normalized_outside_slm(self) -> None:
        structured = json.dumps(
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "proposed",
                        "content": "translated model paraphrase",
                        "evidence_marker": None,
                    }
                ]
            }
        )
        completion = {
            "content": structured,
            "provider": "ollama",
            "model": "test-model",
            "duration_ms": 10.0,
        }
        with patch.object(self.manager, "_structured_slm_completion", return_value=completion):
            result = self.manager.extract_structured_claims(
                content="君は以前、「AにはCの側面がある」と提案した。",
                source_role="user",
            )

        self.assertEqual(result["claims"][0]["subject"], "assistant")
        self.assertEqual(result["claims"][0]["content"], "AにはCの側面がある")

    def test_structured_claim_route_skips_non_attribution_text_before_slm(self) -> None:
        with patch.object(self.manager, "_structured_slm_completion") as completion:
            result = self.manager.extract_structured_claims(
                content="今日は抽出器の速度を測定します。",
                source_role="user",
            )

        completion.assert_not_called()
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["skipped_reason"], "no_attribution_risk")

    def test_temporal_gate_rejects_date_mismatch_and_past_reminder(self) -> None:
        wrong_today = self.manager.validate_response_temporal(
            content="今日は2026年8月16日です。",
            as_of="2026-08-15T12:00:00+09:00",
            timezone="Asia/Tokyo",
        )
        past_reminder = self.manager.validate_response_temporal(
            content="2026年8月13日に買い物へ行くようリマインドします。",
            as_of="2026-08-15T12:00:00+09:00",
            timezone="Asia/Tokyo",
        )
        historical = self.manager.validate_response_temporal(
            content="2026年8月13日に買い物へ行きました。",
            as_of="2026-08-15T12:00:00+09:00",
            timezone="Asia/Tokyo",
        )
        rejected_action = self.manager.validate_response_temporal(
            content="2026年8月13日は過ぎているため、未来のリマインドとしては登録できません。",
            as_of="2026-08-15T12:00:00+09:00",
            timezone="Asia/Tokyo",
        )
        affirmative_with_negative_purpose = self.manager.validate_response_temporal(
            content="忘れないように2026年8月13日にリマインドします。",
            as_of="2026-08-15T12:00:00+09:00",
            timezone="Asia/Tokyo",
        )
        polite_conditional = self.manager.validate_response_temporal(
            content=(
                "承知いたしました。明日（8月13日）の午前9時になりましたら、"
                "買い物の時間だとお知らせします。"
            ),
            as_of="2026-08-16T11:31:00+09:00",
            timezone="Asia/Tokyo",
        )

        self.assertEqual(wrong_today["decision"], "reject")
        self.assertEqual(past_reminder["decision"], "reject")
        self.assertEqual(historical["decision"], "allow")
        self.assertEqual(rejected_action["decision"], "allow")
        self.assertEqual(
            rejected_action["claims"][0]["reason"],
            "future_action_negated",
        )
        self.assertEqual(affirmative_with_negative_purpose["decision"], "reject")
        self.assertEqual(polite_conditional["decision"], "reject")
        self.assertEqual(
            polite_conditional["claims"][0]["reason"],
            "future_action_date_is_past",
        )

    def test_temporal_gate_checks_cross_sentence_and_request_constraints(self) -> None:
        as_of = "2026-08-16T11:41:00+09:00"
        request_content = "明日の予定だけどさ、8/13に買い物に行くから、朝9時になったら教えて"
        split_commitment = self.manager.validate_response_temporal(
            content=(
                "2026年8月13日の件ですね。"
                "午前9時になりましたらお知らせします。"
            ),
            as_of=as_of,
            timezone="Asia/Tokyo",
        )
        inherited_request = self.manager.validate_response_temporal(
            content="承知しました。午前9時になりましたらお知らせします。",
            request_content=request_content,
            as_of=as_of,
            timezone="Asia/Tokyo",
        )
        correction = self.manager.validate_response_temporal(
            content=(
                "8月13日は既に過ぎています。"
                "明日は8月17日ですが、どちらの日付を希望するか確認させてください。"
            ),
            request_content=request_content,
            as_of=as_of,
            timezone="Asia/Tokyo",
        )
        valid_future = self.manager.validate_response_temporal(
            content="8月20日の午前9時になりましたらお知らせします。",
            request_content="8月20日に買い物へ行くので、午前9時に教えて",
            as_of=as_of,
            timezone="Asia/Tokyo",
        )

        self.assertEqual(split_commitment["decision"], "reject")
        self.assertEqual(
            split_commitment["claims"][0]["reason"],
            "future_action_date_is_past",
        )
        self.assertEqual(inherited_request["decision"], "reject")
        self.assertEqual(inherited_request["request_issue_count"], 2)
        self.assertTrue(
            all(
                claim["reason"] == "response_does_not_resolve_request_temporal_conflict"
                for claim in inherited_request["claims"]
            )
        )
        self.assertEqual(correction["decision"], "allow")
        self.assertEqual(correction["request_issue_count"], 2)
        self.assertEqual(valid_future["decision"], "allow")
        self.assertEqual(valid_future["request_issue_count"], 0)

    def test_supersession_marks_prospective_dependents_for_review(self) -> None:
        old_trace = self.manager.create_memory_trace(
            {"content": "Old schedule", "candidate_memory_type": "semantic", "event_time": "2026-08-01T00:00:00Z"}
        )
        new_trace = self.manager.create_memory_trace(
            {"content": "Corrected schedule", "candidate_memory_type": "semantic", "event_time": "2026-08-10T00:00:00Z"}
        )
        old_memory = self.manager.consolidate_memory_trace(old_trace["id"])
        new_memory = self.manager.consolidate_memory_trace(new_trace["id"])
        dependent = self.manager.create_memory_trace(
            {
                "content": "Act on the old schedule tomorrow.",
                "candidate_memory_type": "prospective",
                "derived_from": [
                    {"object_type": "memory", "object_id": old_memory["id"], "relation": "derived_from"}
                ],
            }
        )

        relation = self.manager.supersede_long_term_memory(
            old_memory["id"], new_memory["id"], effective_at="2026-08-10T12:00:00Z"
        )
        reviewed = self.manager.get_memory_trace(dependent["id"])

        self.assertIn(
            {"object_type": "memory_trace", "object_id": dependent["id"]},
            relation["dependent_review"],
        )
        self.assertEqual(reviewed["status"], "review")
        self.assertEqual(reviewed["epistemic_status"], "disputed")

    def test_forget_trace_keeps_terminal_audit_event(self) -> None:
        trace = self.manager.create_memory_trace({"content": "Temporary candidate"})
        result = self.manager.forget_memory_trace(trace["id"], reason="test_cleanup")

        self.assertTrue(result["forgotten"])
        with self.assertRaises(KeyError):
            self.manager.get_memory_trace(trace["id"])
        events = self.manager.list_audit_events(
            object_type="memory_trace", object_id=trace["id"], include_payload=True
        )
        self.assertEqual(events[0]["event_type"], "memory_trace.forgotten")
        self.assertTrue(self.manager.verify_audit()["valid"])


if __name__ == "__main__":
    unittest.main()

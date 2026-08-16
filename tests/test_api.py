from __future__ import annotations

import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class LayeredMemoryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        cls.db_path = TEST_TMP / f"hippocampus-api-{uuid.uuid4().hex}.db"
        cls.previous_db = os.environ.get("HIPPOCAMPUS_DB")
        os.environ["HIPPOCAMPUS_DB"] = str(cls.db_path)

        sys.modules.pop("app", None)
        import app as app_module

        cls.client = TestClient(app_module.app)
        cls.manager = app_module.manager
        cls.security_dir = app_module.manager.layered.security.security_dir

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.previous_db is None:
            os.environ.pop("HIPPOCAMPUS_DB", None)
        else:
            os.environ["HIPPOCAMPUS_DB"] = cls.previous_db
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{cls.db_path}{suffix}")
            if path.exists():
                path.unlink()
        shutil.rmtree(cls.security_dir, ignore_errors=True)

    def test_trace_lifecycle_over_http(self) -> None:
        created = self.client.post(
            "/memory/traces",
            json={
                "conversation_id": "api-conversation",
                "turn_id": "turn-3",
                "trace_stage": "candidate",
                "candidate_memory_type": "episodic",
                "content": "The user showed strong interest in layered memory.",
                "keywords": ["layered memory"],
                "activation": 0.80,
                "salience": 0.90,
                "stability": 0.20,
                "affect_signal": {"valence": "positive", "intensity": 0.88},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        trace = created.json()

        recalled = self.client.post(f"/memory/traces/{trace['id']}/recall")
        self.assertEqual(recalled.status_code, 200, recalled.text)
        self.assertEqual(recalled.json()["recall_count"], 1)

        consolidated = self.client.post(
            f"/memory/traces/{trace['id']}/consolidate",
            json={},
        )
        self.assertEqual(consolidated.status_code, 200, consolidated.text)
        memory = consolidated.json()
        self.assertEqual(memory["memory_type"], "episodic")
        self.assertEqual(memory["epistemic_status"], "inferred")

        retrieved = self.client.post(
            "/memories/retrieve",
            json={"query": "layered memory", "update_recall": False},
        )
        self.assertEqual(retrieved.status_code, 200, retrieved.text)
        ids = [item["memory"]["id"] for item in retrieved.json()["retrieved"]]
        self.assertIn(memory["id"], ids)

    def test_phase1_status_is_complete(self) -> None:
        response = self.client.get("/status/phase1")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["phase"], 1)
        self.assertTrue(body["complete"])
        self.assertEqual(body["missing_objects"], [])
        self.assertTrue(all(body["capabilities"].values()))

    def test_conversation_segmentation_routes(self) -> None:
        conversation_id = "api-segmentation-chat"
        self.manager.ingest_messages(
            conversation_id,
            [
                {
                    "id": "api-segment-u1",
                    "role": "user",
                    "content": "First topic",
                    "event_time": "2026-08-16T09:00:00+00:00",
                },
                {
                    "id": "api-segment-a1",
                    "role": "assistant",
                    "content": "First response",
                    "event_time": "2026-08-16T09:01:00+00:00",
                },
                {
                    "id": "api-segment-u2",
                    "role": "user",
                    "content": "Later topic",
                    "event_time": "2026-08-16T11:00:00+00:00",
                },
            ],
            auto_capture=False,
        )

        detected = self.client.post(
            "/memory/segments/detect",
            json={
                "conversation_id": conversation_id,
                "session_gap_minutes": 90,
                "use_slm": False,
                "persist": True,
            },
        )
        self.assertEqual(detected.status_code, 200, detected.text)
        body = detected.json()
        self.assertEqual(body["boundary_count"], 1)
        self.assertEqual(body["segment_count"], 4)

        boundaries = self.client.get(
            "/memory/boundaries", params={"conversation_id": conversation_id}
        )
        self.assertEqual(boundaries.status_code, 200, boundaries.text)
        self.assertEqual(boundaries.json()[0]["after_event_id"], "api-segment-u2")

        segments = self.client.get(
            "/memory/segments",
            params={"conversation_id": conversation_id, "segment_type": "session"},
        )
        self.assertEqual(segments.status_code, 200, segments.text)
        self.assertEqual(len(segments.json()), 2)

        status = self.client.get("/status/segmentation")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["complete"])
        self.assertEqual(status.json()["schema_version"], 8)

    def test_structured_slm_claim_route_accepts_user_or_assistant_input(self) -> None:
        extracted = {
            "format": "hippocampus.structured-claims.v1",
            "source_role": "assistant",
            "claims": [
                {
                    "subject": "user",
                    "predicate": "said",
                    "content": "A was discussed",
                    "evidence_marker": None,
                }
            ],
            "gate_claims": [],
            "provider": "ollama",
            "model": "test-model",
            "duration_ms": 1.0,
            "cached": False,
        }
        with patch.object(self.manager, "extract_structured_claims", return_value=extracted):
            response = self.client.post(
                "/slm/claims/extract",
                json={"source_role": "assistant", "content": "The user discussed A."},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["claims"][0]["subject"], "user")

    def test_phase2_ingest_and_status(self) -> None:
        status = self.client.get("/status/phase2")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["complete"])

        response = self.client.post(
            "/memory/ingest",
            json={
                "conversation_id": "api-phase2-conversation",
                "messages": [
                    {
                        "id": "api-phase2-message",
                        "role": "user",
                        "content": "本当に素晴らしい。とても感動した！",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["automatic_capture"]["created"], 1)
        self.assertEqual(len(body["memory_traces"]), 1)

        trace = self.client.get(f"/memory/traces/{body['memory_traces'][0]}")
        self.assertEqual(trace.status_code, 200, trace.text)
        self.assertEqual(trace.json()["epistemic_status"], "inferred")

    def test_phase3_status_and_temporal_context(self) -> None:
        status = self.client.get("/status/phase3")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["complete"])

        ingested = self.client.post(
            "/memory/ingest",
            json={
                "conversation_id": "api-temporal-conversation",
                "default_timezone": "Asia/Tokyo",
                "auto_capture": False,
                "messages": [
                    {
                        "id": "api-temporal-message",
                        "role": "user",
                        "content": "Historical event",
                        "event_time": "2026-08-01T09:00:00",
                        "timezone": "Asia/Tokyo",
                    }
                ],
            },
        )
        self.assertEqual(ingested.status_code, 200, ingested.text)

        temporal = self.client.get(
            "/temporal/context",
            params={
                "conversation_id": "api-temporal-conversation",
                "timezone": "Asia/Tokyo",
                "as_of": "2026-08-02T00:00:00Z",
            },
        )
        self.assertEqual(temporal.status_code, 200, temporal.text)
        self.assertEqual(temporal.json()["latest_event"]["event_time"], "2026-08-01T00:00:00.000+00:00")

    def test_phase4_status_verification_and_provenance_api(self) -> None:
        ingested = self.client.post(
            "/memory/ingest",
            json={
                "conversation_id": "api-audit-conversation",
                "auto_capture": False,
                "messages": [
                    {
                        "id": "api-audit-source",
                        "role": "user",
                        "content": "Auditable source statement",
                        "actor_id": "api-user",
                        "actor_role": "user",
                        "source_channel": "api-test",
                        "content_origin": "original",
                    }
                ],
            },
        )
        self.assertEqual(ingested.status_code, 200, ingested.text)

        status = self.client.get("/status/phase4")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["complete"])
        self.assertFalse(status.json()["capabilities"]["signed_checkpoints"])

        verification = self.client.get("/audit/verify")
        self.assertEqual(verification.status_code, 200, verification.text)
        self.assertTrue(verification.json()["valid"])

        provenance = self.client.get("/provenance/raw_message/api-audit-source")
        self.assertEqual(provenance.status_code, 200, provenance.text)
        events = provenance.json()["events"]
        self.assertEqual(events[0]["actor_id"], "api-user")
        self.assertEqual(events[0]["actor_role"], "user")
        self.assertEqual(events[0]["source_channel"], "api-test")

        listed = self.client.get(
            "/audit/events",
            params={"object_type": "raw_message", "object_id": "api-audit-source"},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["events"]), 1)

    def test_phase5_checkpoint_key_branch_and_backup_api(self) -> None:
        status = self.client.get("/status/phase5")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["complete"])

        checkpoint = self.client.post(
            "/audit/checkpoints", json={"reason": "api_test"}
        )
        self.assertEqual(checkpoint.status_code, 200, checkpoint.text)
        self.assertTrue(checkpoint.json()["anchor"]["valid"])

        verification = self.client.get("/audit/checkpoints/verify")
        self.assertEqual(verification.status_code, 200, verification.text)
        self.assertTrue(verification.json()["valid"])
        self.assertEqual(verification.json()["uncheckpointed_event_count"], 0)

        keys = self.client.get("/audit/keys")
        branches = self.client.get("/audit/branches")
        self.assertEqual(len(keys.json()["keys"]), 1)
        self.assertEqual(len(branches.json()["branches"]), 1)

        backup = self.client.post("/backups", json={"label": "api-test"})
        self.assertEqual(backup.status_code, 200, backup.text)
        filename = backup.json()["filename"]
        checked = self.client.post("/backups/verify", json={"filename": filename})
        planned = self.client.post("/restores/plan", json={"filename": filename})
        self.assertTrue(checked.json()["valid"])
        self.assertTrue(planned.json()["valid"])
        self.assertEqual(planned.json()["relation"], "same_checkpoint")

    def test_attribution_gate_validation_and_candidate_selection_api(self) -> None:
        ingested = self.client.post(
            "/memory/ingest",
            json={
                "conversation_id": "api-attribution-example",
                "auto_capture": False,
                "messages": [
                    {
                        "id": "api-attribution-user",
                        "role": "user",
                        "content": "A supports interpretation B.",
                        "actor_role": "user",
                    },
                    {
                        "id": "api-attribution-assistant",
                        "role": "assistant",
                        "content": "A may also support interpretation C.",
                        "actor_role": "assistant",
                    },
                ],
            },
        )
        self.assertEqual(ingested.status_code, 200, ingested.text)

        rejected = self.client.post(
            "/attribution/validate",
            json={
                "content": '君が以前述べた「A may also support interpretation C.」は興味深い。',
                "conversation_id": "api-attribution-example",
            },
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["decision"], "reject")

        selected = self.client.post(
            "/response/candidates/select",
            json={
                "conversation_id": "api-attribution-example",
                "candidates": [
                    {
                        "candidate_id": "wrong",
                        "content": 'You said "A may also support interpretation C."',
                    },
                    {
                        "candidate_id": "right",
                        "content": (
                            'I said "A may also support interpretation C."'
                            "[[event:api-attribution-assistant]]"
                        ),
                    },
                ],
            },
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["selected_candidate_id"], "right")
        self.assertNotIn("[[event:", selected.json()["selected_content"])

        status = self.client.get("/status/attribution-gate")
        self.assertTrue(status.json()["complete"])

    def test_candidate_selection_checks_request_temporal_constraints(self) -> None:
        response = self.client.post(
            "/response/candidates/select",
            json={
                "request_content": (
                    "明日の予定だけどさ、8/13に買い物に行くから、"
                    "朝9時になったら教えて"
                ),
                "as_of": "2026-08-16T11:41:00+09:00",
                "timezone": "Asia/Tokyo",
                "candidates": [
                    {
                        "candidate_id": "primary",
                        "content": (
                            "2026年8月13日に買い物に行かれるのですね。"
                            "午前9時になりましたらお知らせします。"
                        ),
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decision"], "regenerate")
        self.assertTrue(
            any(
                item["reason"]
                == "response_does_not_resolve_request_temporal_conflict"
                for item in response.json()["regeneration_feedback"]
            )
        )

    def test_invalid_phase3_timestamp_returns_400(self) -> None:
        response = self.client.post(
            "/memory/ingest",
            json={
                "conversation_id": "invalid-temporal-input",
                "messages": [{"role": "user", "content": "test", "event_time": "not-a-time"}],
            },
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_phase3_supersession_route_preserves_history(self) -> None:
        original = self.client.post(
            "/memory/remember",
            json={
                "content": "The preferred editor is Alpha.",
                "keywords": ["preferred editor"],
                "dedupe": False,
                "event_time": "2026-08-01T00:00:00Z",
                "valid_from": "2026-08-01T00:00:00Z",
            },
        )
        replacement = self.client.post(
            "/memory/remember",
            json={
                "content": "The preferred editor is Beta.",
                "keywords": ["preferred editor"],
                "dedupe": False,
                "event_time": "2026-08-10T00:00:00Z",
                "valid_from": "2026-08-10T00:00:00Z",
            },
        )
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(replacement.status_code, 200, replacement.text)
        original_id = original.json()["memory"]["id"]
        replacement_id = replacement.json()["memory"]["id"]

        superseded = self.client.post(
            f"/memories/{original_id}/supersede",
            json={
                "replacement_memory_id": replacement_id,
                "effective_at": "2026-08-10T00:00:00Z",
            },
        )
        self.assertEqual(superseded.status_code, 200, superseded.text)
        body = superseded.json()
        self.assertEqual(body["superseded"]["superseded_by"], replacement_id)
        self.assertEqual(body["superseded"]["valid_until"], "2026-08-10T00:00:00.000+00:00")

        historical = self.client.post(
            "/memories/retrieve",
            json={
                "query": "preferred editor",
                "as_of": "2026-08-05T00:00:00Z",
                "temporal_scope": "current",
                "update_recall": False,
            },
        )
        self.assertEqual(historical.status_code, 200, historical.text)
        ids = [item["memory"]["id"] for item in historical.json()["retrieved"]]
        self.assertIn(original_id, ids)
        self.assertNotIn(replacement_id, ids)

        not_yet_historical = self.client.post(
            "/memories/retrieve",
            json={
                "query": "preferred editor",
                "as_of": "2026-08-05T00:00:00Z",
                "temporal_scope": "historical",
                "update_recall": False,
            },
        )
        self.assertEqual(not_yet_historical.status_code, 200, not_yet_historical.text)
        ids = [item["memory"]["id"] for item in not_yet_historical.json()["retrieved"]]
        self.assertNotIn(original_id, ids)

    def test_explicit_legacy_route_creates_confirmed_canonical_memory(self) -> None:
        response = self.client.post(
            "/memory/remember",
            json={
                "content": "Remember that sensor observations must preserve perspective.",
                "keywords": ["sensor", "perspective"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        memory_id = response.json()["memory"]["id"]

        canonical = self.client.get(f"/memories/{memory_id}")
        self.assertEqual(canonical.status_code, 200, canonical.text)
        body = canonical.json()
        self.assertEqual(body["memory_type"], "semantic")
        self.assertEqual(body["acquisition_mode"], "user_explicit")
        self.assertEqual(body["epistemic_status"], "confirmed")
        self.assertEqual(body["epistemic_confidence"], 1.0)

        context = self.client.post(
            "/context/build",
            json={"query": "sensor perspective"},
        )
        self.assertEqual(context.status_code, 200, context.text)
        self.assertIn("sensor observations must preserve perspective", context.json()["memory_context"])

    def test_invalid_trace_threshold_order_returns_400(self) -> None:
        response = self.client.post(
            "/memory/traces",
            json={
                "content": "Invalid thresholds",
                "delete_threshold": 0.7,
                "record_threshold": 0.6,
                "review_threshold": 0.8,
            },
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_observation_contract_keeps_report_and_hypothesis_separate(self) -> None:
        response = self.client.post(
            "/memory/traces",
            json={
                "content": "Front range reading",
                "candidate_memory_type": "embodied",
                "epistemic_status": "observed",
                "observation_statement": "front_range reported 1.20 m",
                "perspective": "sensor:front_range",
                "evidence_kind": "direct_measurement",
                "observation_fidelity": 1.0,
                "source_reliability": 0.86,
                "world_hypothesis": "An object may be 1.2 m ahead",
                "epistemic_confidence": 0.78,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["observation_statement"], "front_range reported 1.20 m")
        self.assertEqual(body["observation_fidelity"], 1.0)
        self.assertEqual(body["world_hypothesis"], "An object may be 1.2 m ahead")
        self.assertEqual(body["epistemic_confidence"], 0.78)

    def test_review_and_evidence_routes(self) -> None:
        created = self.client.post(
            "/memory/traces",
            json={
                "trace_stage": "candidate",
                "candidate_memory_type": "semantic",
                "content": "The public memory design keeps inference tentative.",
                "keywords": ["tentative inference"],
                "source_event_ids": ["public-turn-12"],
                "epistemic_confidence": 0.74,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        trace_id = created.json()["id"]

        reviewed = self.client.post(
            f"/memory/traces/{trace_id}/review",
            json={
                "decision": "confirm",
                "notes": "The user reviewed this wording.",
            },
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        body = reviewed.json()
        self.assertEqual(body["trace"]["status"], "consolidated")
        self.assertEqual(body["trace"]["epistemic_status"], "confirmed")
        self.assertEqual(body["memory"]["epistemic_status"], "confirmed")
        self.assertIn("reviewed this wording", body["memory"]["evidence_summary"])

        evidence = self.client.get(f"/memories/{body['memory']['id']}/evidence")
        self.assertEqual(evidence.status_code, 200, evidence.text)
        evidence_body = evidence.json()
        self.assertEqual(evidence_body["source_event_ids"], ["public-turn-12"])
        self.assertEqual(
            {link["relation"] for link in evidence_body["links"]},
            {"consolidated_from", "supported_by"},
        )

        repeated = self.client.post(
            f"/memory/traces/{trace_id}/consolidate",
            json={},
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["id"], body["memory"]["id"])

        conflicting_review = self.client.post(
            f"/memory/traces/{trace_id}/review",
            json={"decision": "archive"},
        )
        self.assertEqual(conflicting_review.status_code, 400, conflicting_review.text)


if __name__ == "__main__":
    unittest.main()

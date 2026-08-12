from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

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

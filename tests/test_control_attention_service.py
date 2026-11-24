"""P6/P7 API integration with fictional events in isolated temporary DBs only."""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_v02 as fixtures
from v02.api import create_app
from v02.contracts import uid
from v02.service import Service


PREFIX = "/api/v0.2"
AT = "2026-01-01T10:00:00Z"
DUE = "2026-01-01T11:00:00Z"


class ControlAttentionServiceTests(unittest.TestCase):
    tearDown = fixtures.CanonicalTests.tearDown
    memory = fixtures.CanonicalTests.memory
    context_run = fixtures.CanonicalTests.context_run

    def setUp(self):
        for name in ("v02.service.now", "v02.policy.now", "v02.contracts.now"):
            clock = patch(name, return_value=AT)
            clock.start()
            self.addCleanup(clock.stop)
        fixtures.CanonicalTests.setUp(self)
        credentials = []
        for token, owner, host, permissions in (
            ("demo-controller", self.p, "demo:host", ["read", "write", "sync", "observe", "control"]),
            ("demo-host", self.p, "demo:host", ["read", "write", "sync", "observe"]),
            ("demo-other-host", self.p, "demo:other-host", ["read", "control"]),
            ("demo-other-owner", "demo:other-owner", "demo:host", ["read", "control"]),
        ):
            credentials.append({"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                "principal_id": owner, "host_id": host, "permissions": permissions})
        self.client = TestClient(create_app(self.service, credentials))
        self.addCleanup(self.client.close)

    def call(self, method, route, *, body=None, token="demo-controller", status=200):
        response = self.client.request(method, PREFIX + route, json=body,
            headers={"Authorization": "Bearer " + token} if token else {})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def control(self, key="demo:control", **changes):
        return {"idempotency_key": key, "reason": "Fictional structured control assessment",
                "expected_version": None, "scope": {"conversation_id": "chat", "context_key": "conversation"},
                "path_revision": "r1", "expiry": DUE, "evidence_refs": ["u1"], **changes}

    def assess(self, body, *, state="demo:state", **changes):
        return self.call("POST", "/control/assessments/" + state, body=body, **changes)

    def run_context(self, key="demo:run", **changes):
        return self.call("POST", "/context/runs", body={"conversation_id": "chat", "path_revision": "r1",
            "query": "lighthouse", "idempotency_key": key, **changes})

    def proposal(self, **changes):
        return {"scope": {"conversation_id": "chat", "context_key": "conversation"},
            "path_revision": "r1", "direction": ["harbor"], "candidate_refs": [{"id": "m2", "version": 1}],
            "graph_hops": 2, "node_budget": 10, "risk": "uncertain", "reason": "Fictional shadow suggestion",
            "model_id": "demo:model", **changes}

    def counts(self):
        with self.service.db.connection() as con:
            return (dict((row["kind"], row["n"]) for row in con.execute(
                "SELECT kind, COUNT(*) n FROM records GROUP BY kind")),
                con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])

    def stable_records(self):
        with self.service.db.connection() as con:
            return [dict(row) for row in con.execute(
                "SELECT * FROM records WHERE kind NOT IN ('run', 'receipt') ORDER BY id")]

    def sync(self, messages, ids, *, revision="r2", expected="r1", conversation="chat", principal=None):
        return self.service.sync(principal or self.p, {"conversation_id": conversation, "messages": messages,
            "active_event_ids": ids, "revision_key": revision, "expected_revision": expected, "auto_capture": False})

    def test_control_permission_is_separate_from_normal_host_permissions(self):
        request = self.control(prediction_error=.6)
        before = self.counts()
        self.assess(request, token=None, status=401)
        self.assess(request, token="demo-host", status=403)
        self.assess(request, token="demo-other-owner", status=404)
        self.assertEqual(self.counts(), before)
        stored = self.assess(request)
        self.assertEqual(stored["kind"], "state")
        self.assertEqual(stored["version"], 1)
        self.call("GET", "/records/" + stored["id"], token="demo-other-owner", status=404)

    def test_assessment_commits_provenance_and_only_proposes_unresolved_change(self):
        memory = self.memory()
        unresolved = self.service.unresolved(self.p, "demo:unresolved", {
            "idempotency_key": "demo:unresolved-create", "reason": "Fictional open question",
            "conversation_id": "chat", "status": "active", "description": "Which beacon was observed?"})
        stored = self.assess(self.control(prediction_error=.9, unknown_fraction=.7, storage_pressure=.4,
            expected_information_gain=.01, unresolved_status="active"))
        data = stored["data"]
        self.assertEqual(data["suggested_unresolved_status"], "deferred")
        self.assertEqual(data["values"]["uncertainty_load"], .9)
        self.assertIn("preserve_storage_margin", data["preservation_constraints"])
        self.assertEqual(data["path_revision"], "r1")
        self.assertEqual(data["provenance"]["source"], "authorized_host:demo:host")
        self.assertEqual(data["provenance"]["confidence_target"], "functional_control")
        self.assertIsNone(data["provenance"]["confidence"])
        self.assertEqual(data["provenance"]["created_at"], AT)
        self.assertEqual(data["provenance"]["expiry"], DUE)
        self.assertEqual(data["provenance"]["cause_refs"], ["u1"])
        self.assertEqual(self.service.get(self.p, memory["id"]), memory)
        self.assertEqual(self.service.get(self.p, unresolved["id"])["data"], unresolved["data"])
        self.assertEqual(self.service.list_records(self.p, "edge"), [])
        self.assertTrue(self.service.db.verify()["valid"])

    def test_control_replay_host_binding_and_version_conflicts_are_atomic(self):
        request = self.control(prediction_error=.6)
        first = self.assess(request)
        before = self.counts()
        self.assertEqual(self.assess(request), first)
        self.assess(request, token="demo-other-host", status=409)
        self.assess({**request, "prediction_error": .7}, status=409)
        self.assess({**request, "idempotency_key": "demo:stale"}, status=409)
        self.assertEqual(self.counts(), before)
        updated = self.assess({**request, "idempotency_key": "demo:update", "expected_version": 1,
                               "prediction_error": .2})
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["data"]["values"]["uncertainty_load"], .2)

    def test_control_rejects_stale_path_wrong_conversation_and_missing_evidence(self):
        self.sync([{"id": "demo:other-event", "role": "user", "content": "Fictional separate context"}],
                  ["demo:other-event"], revision="r1", expected=None, conversation="demo:other-chat")
        for index, (changes, expected_status) in enumerate((
            ({"path_revision": "demo:old-path"}, 409),
            ({"evidence_refs": ["demo:other-event"]}, 422),
            ({"evidence_refs": ["demo:missing"]}, 404),
            ({"expiry": AT}, 422),
            ({"assessment": {"target": "world_claim", "score": 1.}}, 422),
        )):
            with self.subTest(changes=changes):
                before = self.counts()
                self.assess(self.control("demo:bad-" + str(index), **changes), status=expected_status)
                self.assertEqual(self.counts(), before)

    def test_control_evidence_must_be_on_path_and_in_same_sandbox(self):
        fictional = {"id": "demo:fiction", "role": "user", "sandbox_id": "demo:story", "content": "Fictional story beacon"}
        alternate = {"id": "demo:alternate", "role": "user", "content": "Fictional alternate path"}
        self.sync([fictional, alternate], ["demo:fiction", "demo:alternate"])
        before = self.counts()
        self.assess(self.control(path_revision="r2"), status=409)
        self.assess(self.control("demo:fiction-boundary", path_revision="r2", evidence_refs=["demo:fiction"]), status=409)
        self.assertEqual(self.counts(), before)
        stored = self.assess(self.control("demo:fiction-valid", path_revision="r2", evidence_refs=["demo:fiction"],
            scope={"conversation_id": "chat", "sandbox_id": "demo:story"}))
        self.assertEqual(stored["data"]["scope"]["sandbox_id"], "demo:story")

    def test_assessed_state_is_scoped_expires_and_remains_inert_after_restart(self):
        self.memory()
        state = self.assess(self.control(prediction_error=1., expected_information_gain=1.,
            scope={"conversation_id": "chat", "context_key": "demo:task"}))
        ordinary = self.run_context("demo:ordinary")
        active = self.run_context("demo:active", context_key="demo:task")
        self.assertEqual(ordinary["data"]["plan"]["state_ids"], [])
        self.assertEqual(active["data"]["plan"]["state_ids"], [state["id"]])
        self.assertEqual(active["data"]["plan"]["graph_hops"], 2)
        before = deepcopy(self.service.get(self.p, state["id"])["data"])
        with patch("v02.service.now", return_value=DUE), patch("v02.policy.now", return_value=DUE):
            expired = self.run_context("demo:expired", context_key="demo:task")
            self.assertEqual(expired["data"]["plan"]["state_ids"], [])
            self.assertEqual(expired["data"]["plan"]["graph_hops"], 0)
            restarted = Service(self.folder / "c.db")
            after = restarted.build_run(self.p, {"conversation_id": "chat", "path_revision": "r1",
                "query": "lighthouse", "idempotency_key": "demo:after-restart", "context_key": "demo:task"})
            self.assertEqual(after["data"]["plan"]["state_ids"], [])
            self.assertEqual(restarted.get(self.p, state["id"])["data"], before)
            self.assertFalse(restarted.get(self.p, state["id"])["effective"])

    def test_assessed_state_follows_evidence_membership_not_literal_revision_equality(self):
        self.assess(self.control(prediction_error=1., expected_information_gain=1.))
        self.assertEqual(self.run_context("demo:first")["data"]["plan"]["graph_hops"], 2)
        later = {"id": "demo:later", "role": "user", "content": "Fictional follow-up"}
        self.sync([later], ["u1", "demo:later"])
        continued = self.run_context("demo:descendant", path_revision="r2")
        self.assertEqual(continued["data"]["plan"]["state_ids"], ["demo:state"])
        self.sync([], ["demo:later"], revision="r3", expected="r2")
        changed = self.run_context("demo:alternate", path_revision="r3")
        self.assertEqual(changed["data"]["plan"]["state_ids"], [])
        self.assertEqual(changed["data"]["plan"]["graph_hops"], 0)
        self.assertEqual(self.service.get(self.p, "demo:state")["version"], 1)

    def test_control_change_invalidates_attempt_and_dispatch_but_other_context_does_not(self):
        self.memory()
        self.assess(self.control(prediction_error=.4))
        run = self.run_context("demo:before-control-change")
        # An independent task's control update cannot invalidate this context.
        self.assess(self.control("demo:other-task", cognitive_influence=1.,
            scope={"conversation_id": "chat", "context_key": "demo:other-task"}), state="demo:other-state")
        attempt = {"id": "demo:attempt", "run_id": run["id"], "path_revision": "r1",
                   "model_id": "demo:model", "packet_digest": run["data"]["packet_digest"]}
        self.call("POST", "/attempts", body=attempt)
        self.call("POST", "/attempts/demo:attempt/observations", body={"stage": "host_finalized",
            "status": "retained", "request_digest": "a" * 64, "packet_digest": run["data"]["packet_digest"],
            "path_revision": "r1"})
        self.assess(self.control("demo:updated-control", expected_version=1, prediction_error=.9))
        before = self.counts()
        self.call("POST", "/attempts", body={**attempt, "id": "demo:stale-attempt"}, status=409)
        self.call("POST", "/attempts/demo:attempt/observations", body={"stage": "dispatch",
            "status": "started", "request_digest": "a" * 64, "path_revision": "r1"}, status=409)
        self.assertEqual(self.counts(), before)
        # The historical final-input observation remains, without a fake dispatch.
        observations = self.service.list_records(self.p, "observation", "chat")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["data"]["stage"], "host_finalized")

    def test_shadow_recommendation_does_not_change_packet_selection_or_persistent_state(self):
        self.memory()
        self.memory(id="m2", content="Fictional unrelated harbor record.")
        baseline = self.run_context("demo:baseline")
        before = self.stable_records()
        shadow = self.run_context("demo:shadow", attention_proposal=self.proposal())
        observation = shadow["data"]["attention_shadow"]
        self.assertTrue(observation["proposal_accepted"])
        self.assertEqual(observation["added_candidate_refs"], [{"id": "m2", "version": 1}])
        self.assertEqual(observation["removed_candidate_refs"], [{"id": "m1", "version": 1}])
        self.assertEqual(observation["graph_hops_delta"], 2)
        self.assertFalse(observation["affects_normal_selection"])
        self.assertEqual(shadow["data"]["plan"], baseline["data"]["plan"])
        self.assertEqual(shadow["data"]["selected"], baseline["data"]["selected"])
        self.assertEqual(shadow["data"]["packet_digest"], baseline["data"]["packet_digest"])
        self.assertEqual(shadow["data"]["prompt_text"], baseline["data"]["prompt_text"])
        self.assertEqual(self.stable_records(), before)
        self.assertTrue(self.service.db.verify()["valid"])

    def test_invalid_shadow_uid_version_or_authority_falls_back_without_raw_output(self):
        self.memory()
        self.memory(id="m2", content="Fictional unrelated harbor record.")
        self.service.update_memory(self.p, "m2", {"idempotency_key": "demo:edit", "expected_version": 1,
            "reason": "Fictional revised record", "content": "Fictional revised harbor record."}, "content")
        baseline = self.run_context("demo:baseline")
        for index, changes in enumerate((
            {}, {"candidate_refs": [{"id": "demo:unknown", "version": 1}]},
            {"path_revision": "demo:old-path"}, {"scope": {"conversation_id": "demo:other"}},
            {"graph_hops": 8}, {"instructions": "DEMO_RAW_OUTPUT_MUST_NOT_BE_STORED"},
        )):
            with self.subTest(changes=changes):
                result = self.run_context("demo:bad-shadow-" + str(index), attention_proposal=self.proposal(**changes))
                self.assertEqual(result["data"]["attention_shadow"]["fallback_reason"], "invalid_proposal")
                self.assertEqual(result["data"]["selected"], baseline["data"]["selected"])
                self.assertEqual(result["data"]["packet_digest"], baseline["data"]["packet_digest"])
                self.assertNotIn("DEMO_RAW_OUTPUT_MUST_NOT_BE_STORED", str(result))

    def test_cross_conversation_memory_can_retrieve_normally_but_is_not_relabelled_into_shadow_pool(self):
        self.sync([{"id": "demo:other-event", "role": "user", "content": "Fictional second lighthouse"}],
            ["demo:other-event"], revision="r1", expected=None, conversation="demo:other-chat")
        self.memory(id="demo:other-memory", conversation_id="demo:other-chat", source_event_ids=["demo:other-event"])
        baseline = self.run_context("demo:cross-baseline")
        self.assertIn("demo:other-memory", baseline["data"]["compiled_memory_ids"])
        result = self.run_context("demo:cross-shadow", attention_proposal=self.proposal(
            candidate_refs=[{"id": "demo:other-memory", "version": 1}]))
        self.assertFalse(result["data"]["attention_shadow"]["proposal_accepted"])
        self.assertEqual(result["data"]["selected"], baseline["data"]["selected"])
        self.assertEqual(result["data"]["attention_shadow"]["baseline"]["candidate_refs"], [])

    def test_shadow_run_replay_is_idempotent_and_different_proposal_conflicts(self):
        self.memory()
        self.memory(id="m2", content="Fictional unrelated harbor record.")
        request = self.proposal()
        first = self.run_context("demo:replay", attention_proposal=request)
        before = self.counts()
        self.assertEqual(self.run_context("demo:replay", attention_proposal=request), first)
        self.call("POST", "/context/runs", body={"conversation_id": "chat", "path_revision": "r1",
            "query": "lighthouse", "idempotency_key": "demo:replay", "attention_proposal": {**request, "reason": "Changed"}}, status=409)
        self.assertEqual(self.counts(), before)
        receipt = self.service.get(self.p, uid("receipt", self.p, "context.build", "demo:replay"))
        self.assertEqual(receipt["data"]["result"], first)


if __name__ == "__main__":
    unittest.main()

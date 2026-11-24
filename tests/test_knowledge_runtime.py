"""K4 runtime boundaries with fictional events and isolated temporary databases.

No live service, dialogue, model, configuration, or runtime log is accessed.
"""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

import test_knowledge_service as fixtures
from v02.contracts import digest
from v02.service import Service


PREFIX = fixtures.PREFIX
KNOWLEDGE = fixtures.KNOWLEDGE
EVALUATED = "2026-01-01T10:15:00Z"
STATE_EXPIRY = "2026-01-01T10:16:00Z"


class KnowledgeRuntimeTests(unittest.TestCase):
    remove_fixture = fixtures.KnowledgeServiceTests.remove_fixture
    call = fixtures.KnowledgeServiceTests.call
    sync = fixtures.KnowledgeServiceTests.sync
    command = fixtures.KnowledgeServiceTests.command
    ack_ref = fixtures.KnowledgeServiceTests.ack_ref
    source = fixtures.KnowledgeServiceTests.source
    create_claim = fixtures.KnowledgeServiceTests.create_claim
    claim = fixtures.KnowledgeServiceTests.claim
    claim_command = fixtures.KnowledgeServiceTests.claim_command
    evidence_spec = fixtures.KnowledgeServiceTests.evidence_spec
    replace = fixtures.KnowledgeServiceTests.replace
    search = fixtures.KnowledgeServiceTests.search
    review_request = fixtures.KnowledgeServiceTests.review_request
    review = fixtures.KnowledgeServiceTests.review
    counts = fixtures.KnowledgeServiceTests.counts

    def setUp(self):
        fixtures.KnowledgeServiceTests.setUp(self)
        self.runtime_clocks = []
        for module in ("v02.service", "v02.contracts", "v02.policy", "v02.knowledge_retrieval"):
            clock = patch(module + ".now", return_value=EVALUATED)
            self.runtime_clocks.append(clock.start())
            self.addCleanup(clock.stop)
        for credential in self.client.app.state.credentials:
            if credential["token_sha256"] == hashlib.sha256(b"demo-reviewer").hexdigest():
                credential["permissions"] += ["observe", "control"]
        self.client.app.state.credentials.append({
            "token_sha256": hashlib.sha256(b"demo-other-host").hexdigest(),
            "principal_id": self.principal, "host_id": "demo:second-host", "permissions": ["observe"]})
        self.client.app.state.credentials.append({
            "token_sha256": hashlib.sha256(b"demo-other-observer").hexdigest(),
            "principal_id": "demo:other-owner", "host_id": "demo:host", "permissions": ["observe"]})

    def proposition(self):
        value = fixtures.KnowledgeServiceTests.proposition(self)
        value.update(conditions=[], valid_from=None, valid_until=None)
        return value

    def adopted(self):
        claim_id, source = self.create_claim()
        evidence = self.replace(claim_id, [self.evidence_spec(source)])
        search = self.search(claim_id)
        self.review(claim_id, evidence, search)
        return claim_id, source, evidence, search

    def run_request(self, **values):
        self.serial += 1
        return {"conversation_id": "demo:chat", "path_revision": "r1", "query": "blue beacon",
                "idempotency_key": f"demo:runtime-run-{self.serial}", **values}

    def context_run(self, *, token="demo-reader", status=200, **values):
        return self.call("POST", PREFIX + "/context/runs", body=self.run_request(**values),
                         token=token, status=status)

    def attempt_body(self, run, **values):
        self.serial += 1
        return {"id": f"demo:runtime-attempt-{self.serial}", "run_id": run["id"],
                "path_revision": run["data"]["path_revision"], "model_id": "demo:unsent-model",
                "packet_digest": run["data"]["packet_digest"], **values}

    def attempt(self, run, *, status=200, token="demo-reviewer", **values):
        return self.call("POST", PREFIX + "/attempts", body=self.attempt_body(run, **values),
                         token=token, status=status)

    def observation(self, run, attempt, *, stage="host_finalized", observed="retained", status=200,
                    token="demo-reviewer", **values):
        body = {"stage": stage, "status": observed, "request_digest": "a" * 64,
                "path_revision": run["data"]["path_revision"], **values}
        if stage == "host_finalized" and observed == "retained":
            body.setdefault("packet_digest", run["data"]["packet_digest"])
        return self.call("POST", PREFIX + f"/attempts/{attempt['id']}/observations", body=body,
                         token=token, status=status)

    def plan(self, claim_id):
        return self.service.countersearch.create_plan(self.identity, claim_id,
            self.claim_command(claim_id, queries=[{"purpose": purpose, "text": "demo nonexistent concern",
                "reason": "Additional fictional countercheck requires a new explicit review"} for purpose in
                ("falsification", "alternative_explanation", "boundary_case", "source_correction")]))

    def state(self, *, expected=None, value=.4):
        self.serial += 1
        return self.call("PUT", PREFIX + "/states/demo:runtime-control", body={
            "idempotency_key": f"demo:control-{self.serial}", "reason": "Fictional temporary control state",
            "expected_version": expected, "scope": {"conversation_id": "demo:chat"},
            "expiry": STATE_EXPIRY, "values": {"caution_gain": value}})

    def episode(self, identity, **values):
        return self.call("POST", PREFIX + "/memories", body={
            "idempotency_key": "demo:create-" + identity, "reason": "Fictional episode runtime fixture",
            "memory": {"id": identity, "conversation_id": "demo:chat", "content": "Demo blue beacon episode",
                       "source_event_ids": ["demo:user"], **values}})

    def set_time(self, stamp):
        for clock in self.runtime_clocks:
            clock.return_value = stamp

    def canonical_snapshot(self):
        with self.service.db.connection() as con:
            return digest({"records": [tuple(row) for row in con.execute(
                "SELECT id,kind,version,data_json,latest_audit_event_id FROM records ORDER BY id")],
                "audit": [tuple(row) for row in con.execute("SELECT * FROM audit_events ORDER BY sequence")]})

    def test_disabled_has_no_selection_or_injection_and_api_cannot_enable_it(self):
        claim_id, _, _, _ = self.adopted()
        self.assertEqual(self.service.knowledge_mode, "disabled")
        before = deepcopy(self.claim(claim_id))
        with patch.object(self.service.knowledge_retrieval, "select", side_effect=AssertionError("Disabled resolver called")):
            run = self.context_run()
        data = run["data"]
        self.assertEqual(data["knowledge_selection"], [])
        self.assertIsNone(data["knowledge_manifest"])
        self.assertEqual(data["compiled_claims"], [])
        self.assertEqual(data["packet"]["dictionary_claims"], [])
        self.assertEqual(data["packet"]["knowledge_caveats"], [])
        for forged in ({"knowledge_mode": "enabled"}, {"knowledge_query": {"mode": "enabled"}},
                       {"knowledge_query": {"conditions_satisfied": True}}):
            with self.subTest(forged=forged):
                self.context_run(status=422, **forged)
        status = self.call("GET", PREFIX + "/status", token="demo-reader")
        self.assertEqual(status["knowledge"]["mode"], "disabled")
        self.assertFalse(status["knowledge"]["context_injection"])
        self.assertTrue(status["dispatch_validation_required"])
        self.assertEqual(self.claim(claim_id), before)
        with self.assertRaises(ValueError):
            Service(self.folder / "demo-invalid-mode.db", knowledge_mode="automatic")

    def test_shadow_observes_selection_without_compilation_and_does_not_block_on_new_knowledge(self):
        claim_id, _, evidence, search = self.adopted()
        self.service.knowledge_mode = "shadow"
        run = self.context_run()
        data = run["data"]
        self.assertEqual(data["knowledge_selection"][0]["claim_ref"]["id"], claim_id)
        self.assertIsNotNone(data["knowledge_manifest"])
        self.assertEqual(data["compiled_claims"], [])
        self.assertEqual(data["packet"]["dictionary_claims"], [])
        self.assertEqual(data["metrics"]["knowledge_shadow_selected_count"], 1)
        self.review(claim_id, evidence, search, action="withdraw")
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        self.assertEqual(final["data"]["claim_snapshots"], [])
        self.observation(run, attempt, stage="dispatch", observed="started")
        self.assertEqual(self.claim(claim_id)["data"]["knowledge_state"], "withdrawal_candidate")

    def test_enabled_keeps_claims_and_caveats_distinct_through_retained_observation(self):
        claim_id, _, evidence, _ = self.adopted()
        provisional_id, _ = self.create_claim()
        self.service.knowledge_mode = "enabled"
        before = {identity: deepcopy(self.claim(identity)) for identity in (claim_id, provisional_id)}
        run = self.context_run()
        data = run["data"]
        self.assertEqual(data["packet"]["version"], "hippocampus.packet.v0.3")
        self.assertEqual(len(data["knowledge_selection"]), 2)
        self.assertEqual(data["selected"], [])
        self.assertEqual(data["compiled_memory_ids"], [])
        self.assertEqual(len(data["compiled_claims"]), 2)
        claim = data["packet"]["dictionary_claims"][0]
        caveat = data["packet"]["knowledge_caveats"][0]
        self.assertEqual(claim["claim_ref"]["id"], claim_id)
        self.assertEqual(claim["evidence_refs"], evidence)
        self.assertEqual(claim["source_material"][0]["actor_id"], "demo:narrator")
        self.assertEqual(caveat["claim_ref"]["id"], provisional_id)
        self.assertEqual(caveat["usage_scope"], "unadopted_do_not_assert_as_truth")
        self.assertEqual(caveat["proposition"]["text"], self.proposition()["text"])
        self.assertIn("source_evidence", data["packet"]["usage_contract"]["episode_evidence_sections"])
        self.assertIn("dictionary_claims", data["prompt_text"])
        self.assertIn("unadopted_do_not_assert_as_truth", data["prompt_text"])
        self.assertNotIn("query", data)
        self.assertNotIn("query", data["knowledge_manifest"])
        self.assertEqual(self.service.list_records(self.principal, "observation"), [])
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        self.assertEqual(final["data"]["claim_snapshots"], data["compiled_claims"])
        self.assertEqual(final["data"]["memory_ids"], [])
        self.assertEqual(final["data"]["model_usage"], "unknown")
        self.assertEqual(self.observation(run, attempt), final)
        dispatch = self.observation(run, attempt, stage="dispatch", observed="started")
        self.assertEqual(dispatch["data"]["claim_snapshots"], [])
        self.observation(run, attempt, stage="dispatch", observed="completed")
        for identity, original in before.items():
            self.assertEqual(self.claim(identity), original)

    def test_omitted_and_capacity_blocked_packets_never_count_as_claim_retention(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        for observed in ("not_present", "blocked_capacity", "unknown"):
            with self.subTest(observed=observed):
                attempt = self.attempt(run)
                final = self.observation(run, attempt, observed=observed)
                self.assertEqual(final["data"]["claim_snapshots"], [])
                self.assertEqual(final["data"]["source_ids"], [])
                self.assertEqual(final["data"]["model_usage"], "unknown")
                expected = 200 if observed == "not_present" else 409
                self.observation(run, attempt, stage="dispatch", observed="started", status=expected)

    def test_attempt_rechecks_withdrawal_and_claim_version_without_rewriting_old_run(self):
        self.service.knowledge_mode = "enabled"
        for mutation in ("withdraw", "edit", "evidence"):
            with self.subTest(mutation=mutation):
                claim_id, source, evidence, search = self.adopted()
                run = self.context_run()
                original = deepcopy(run)
                if mutation == "withdraw":
                    self.review(claim_id, evidence, search, action="withdraw")
                elif mutation == "edit":
                    proposition = self.proposition()
                    proposition.update(text="The demo narrator reported a red beacon.", object="red")
                    self.call("PATCH", KNOWLEDGE + f"/claims/{claim_id}",
                              body=self.claim_command(claim_id, proposition=proposition))
                else:
                    self.replace(claim_id, [self.evidence_spec(source, span_start=0, span_end=12)])
                self.attempt(run, status=409)
                self.assertEqual(self.service.get(self.principal, run["id"]), original)
        self.assertEqual(self.service.list_records(self.principal, "attempt"), [])

    def test_dispatch_rechecks_withdrawal_after_retention_and_recompile_uses_caveat(self):
        claim_id, _, evidence, search = self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        self.review(claim_id, evidence, search, action="withdraw")
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        self.assertEqual(self.service.get(self.principal, final["id"]), final)
        self.assertEqual(final["data"]["claim_snapshots"][0]["claim_ref"]["version"], 3)
        current = self.context_run()
        self.assertEqual(current["data"]["packet"]["dictionary_claims"], [])
        self.assertEqual(current["data"]["packet"]["knowledge_caveats"][0]["knowledge_state"], "withdrawal_candidate")

    def test_additional_search_without_claim_version_change_invalidates_attempt_and_dispatch(self):
        claim_id, _, _, _ = self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        self.observation(run, attempt)
        before = deepcopy(self.claim(claim_id))
        self.plan(claim_id)
        self.assertEqual(self.claim(claim_id), before)
        self.attempt(run, status=409)
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        current = self.context_run()
        self.assertIn("new_information_requires_review",
                      current["data"]["packet"]["knowledge_caveats"][0]["reasons"])

    def test_review_expiry_is_rechecked_before_attempt_and_dispatch(self):
        claim_id, _, _, _ = self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        self.observation(run, attempt)
        self.set_time(fixtures.DUE)
        self.attempt(run, status=409)
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        self.assertEqual(self.claim(claim_id)["data"]["knowledge_state"], "established")
        self.assertEqual(self.claim(claim_id)["data"]["assessment"]["score"], .7)

    def test_expired_functional_state_requires_recompile_even_when_knowledge_is_omitted(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        state = self.state()
        run = self.context_run()
        attempt = self.attempt(run)
        self.observation(run, attempt, observed="not_present")
        self.set_time(STATE_EXPIRY)
        self.attempt(run, status=409)
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        fresh = self.context_run()
        self.assertEqual(fresh["data"]["control_manifest"], [])
        self.assertEqual(fresh["data"]["packet"]["response_settings"]["caution_gain"], 0)
        historical = self.service.get(self.principal, state["id"])
        self.assertFalse(historical["effective"])
        self.assertEqual(historical["data"], state["data"])
        self.assertEqual(historical["version"], state["version"])

    def test_state_version_and_path_changes_block_new_send_but_leave_observations_readable(self):
        self.service.knowledge_mode = "enabled"
        self.adopted()
        self.state()
        run = self.context_run()
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        self.state(expected=1, value=.8)
        self.attempt(run, status=409)
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        current = self.context_run()
        second = self.attempt(current)
        self.observation(current, second)
        self.sync([], ["demo:tool"], revision="r2", expected="r1")
        self.attempt(current, status=409)
        self.observation(current, second, stage="dispatch", observed="started", status=409)
        self.assertEqual(self.service.get(self.principal, final["id"]), final)

    def test_selected_episode_expiry_invalidates_attempt_and_dispatch_without_erasing_history(self):
        memory = self.episode("demo:expiring-episode", valid_until=STATE_EXPIRY)
        run = self.context_run()
        self.assertEqual(run["data"]["compiled_memory_ids"], [memory["id"]])
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        self.set_time("2026-01-01T10:17:00Z")
        rejected = self.attempt(run, status=409)
        self.assertIn("Episode applicability changed", rejected["detail"])
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        self.assertEqual(self.service.get(self.principal, memory["id"]), memory)
        self.assertEqual(self.service.get(self.principal, final["id"]), final)
        self.assertEqual(self.context_run()["data"]["compiled_memory_ids"], [])
        historical = self.context_run(temporal_scope="historical")
        self.assertEqual(historical["data"]["compiled_memory_ids"], [memory["id"]])
        self.attempt(historical)

    def test_parent_episode_expiry_does_not_invalidate_a_timeless_derived_episode(self):
        parent = self.episode("demo:parent-episode", content="Demo instrument observation", valid_until=STATE_EXPIRY)
        child = self.episode("demo:child-episode", source_event_ids=[], source_memory_ids=[parent["id"]])
        run = self.context_run()
        self.assertEqual(run["data"]["compiled_memory_ids"], [child["id"]])
        self.assertEqual({ref["id"] for ref in run["data"]["episode_dependencies"]}, {parent["id"], child["id"]})
        self.set_time("2026-01-01T10:17:00Z")
        attempt = self.attempt(run)
        self.observation(run, attempt)
        self.observation(run, attempt, stage="dispatch", observed="started")
        self.assertEqual(self.service.get(self.principal, parent["id"]), parent)

    def test_late_historical_observations_keep_original_claim_refs_after_withdrawal_and_path_change(self):
        claim_id, _, evidence, search = self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        pending = self.attempt(run)
        started = self.attempt(run)
        self.observation(run, started)
        self.observation(run, started, stage="dispatch", observed="started")
        self.review(claim_id, evidence, search, action="withdraw")
        self.sync([], ["demo:tool"], revision="r2", expected="r1")
        late = self.observation(run, pending)
        self.assertEqual(late["data"]["path_revision"], "r1")
        self.assertEqual(late["data"]["claim_snapshots"], run["data"]["compiled_claims"])
        self.assertEqual(late["data"]["model_usage"], "unknown")
        self.observation(run, pending, stage="dispatch", observed="started", status=409)
        completion = self.observation(run, started, stage="dispatch", observed="completed")
        self.assertEqual(completion["data"]["claim_snapshots"], [])
        self.assertEqual(self.claim(claim_id)["data"]["knowledge_state"], "withdrawal_candidate")

    def test_omitted_packet_can_dispatch_after_only_knowledge_changes_without_claim_credit(self):
        claim_id, _, evidence, search = self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        self.observation(run, attempt, observed="not_present")
        self.review(claim_id, evidence, search, action="withdraw")
        dispatch = self.observation(run, attempt, stage="dispatch", observed="started")
        self.assertEqual(dispatch["data"]["claim_snapshots"], [])

    def test_observation_auth_digest_and_caller_supplied_claim_snapshots_are_enforced(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        self.attempt(run, token="demo-reader", status=403)
        self.attempt(run, token="demo-other-owner", status=403)
        self.attempt(run, token="demo-other-observer", status=404)
        self.attempt(run, packet_digest="b" * 64, status=409)
        attempt = self.attempt(run)
        self.observation(run, attempt, token="demo-other-host", status=409)
        self.observation(run, attempt, packet_digest="b" * 64, status=409)
        self.observation(run, attempt, claim_snapshots=run["data"]["compiled_claims"], status=422)
        self.assertEqual(self.service.list_records(self.principal, "observation"), [])
        final = self.observation(run, attempt)
        self.assertEqual(final["data"]["claim_snapshots"], run["data"]["compiled_claims"])
        self.observation(run, attempt, stage="dispatch", observed="started", request_digest="c" * 64, status=409)

    def test_historical_packet_contract_is_readable_but_cannot_start_a_new_attempt(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        original_get = self.service.db.get

        def legacy_snapshot(*args, **kwargs):
            record = original_get(*args, **kwargs)
            if record["id"] == run["id"]:
                record = deepcopy(record)
                record["data"]["packet"]["version"] = "hippocampus.packet.v0.2"
            return record

        with patch.object(self.service.db, "get", side_effect=legacy_snapshot):
            history = self.call("GET", PREFIX + f"/records/{run['id']}", token="demo-reader")
            self.assertEqual(history["data"]["packet"]["version"], "hippocampus.packet.v0.2")
            rejected = self.attempt(run, status=409)
            self.assertIn("Packet contract changed", rejected["detail"])
        self.assertEqual(self.service.get(self.principal, run["id"]), run)

    def test_restarting_with_injection_disabled_blocks_old_enabled_run_for_new_sends(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        for mode in ("disabled", "shadow"):
            with self.subTest(mode=mode):
                self.service = Service(self.folder / "demo.db", knowledge_mode=mode)
                self.client.app.state.service = self.service
                self.attempt(run, status=409)
                self.observation(run, attempt, stage="dispatch", observed="started", status=409)
                self.assertEqual(self.service.get(self.principal, final["id"]), final)
                fresh = self.context_run()
                self.assertEqual(fresh["data"]["compiled_claims"], [])
                self.assertEqual(fresh["data"]["knowledge_mode"], mode)

    def test_dispatch_start_receipt_cannot_authorize_a_second_unchanged_send(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        final = self.observation(run, attempt)
        started = self.observation(run, attempt, stage="dispatch", observed="started")
        before = self.canonical_snapshot()
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        self.assertEqual(self.observation(run, attempt), final)
        self.assertEqual(self.service.get(self.principal, started["id"]), started)
        self.assertEqual(self.canonical_snapshot(), before)

    def test_dispatch_start_replay_after_withdrawal_is_rejected_without_rewriting_receipts(self):
        claim_id, _, evidence, search = self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        attempt = self.attempt(run)
        self.observation(run, attempt)
        self.observation(run, attempt, stage="dispatch", observed="started")
        self.review(claim_id, evidence, search, action="withdraw")
        before = self.canonical_snapshot()
        self.observation(run, attempt, stage="dispatch", observed="started", status=409)
        self.assertEqual(self.canonical_snapshot(), before)
        self.assertEqual(self.claim(claim_id)["data"]["knowledge_state"], "withdrawal_candidate")

    def test_terminal_dispatch_keeps_history_replay_but_cannot_reauthorize_started(self):
        self.adopted()
        self.service.knowledge_mode = "enabled"
        run = self.context_run()
        for outcome in ("completed", "failed"):
            with self.subTest(outcome=outcome):
                attempt = self.attempt(run)
                self.observation(run, attempt)
                self.observation(run, attempt, stage="dispatch", observed="started")
                terminal = self.observation(run, attempt, stage="dispatch", observed=outcome)
                before = self.canonical_snapshot()
                self.observation(run, attempt, stage="dispatch", observed="started", status=409)
                self.assertEqual(self.observation(run, attempt, stage="dispatch", observed=outcome), terminal)
                self.assertEqual(self.canonical_snapshot(), before)

    def test_api_time_context_preserves_unresolved_applicability_and_cannot_bypass_review_expiry(self):
        proposition = self.proposition()
        proposition.update(valid_from="2025-12-01T00:00:00Z", valid_until="2025-12-02T00:00:00Z")
        claim_id, source = self.create_claim(proposition=proposition)
        evidence = self.replace(claim_id, [self.evidence_spec(source)])
        search = self.search(claim_id)
        self.review(claim_id, evidence, search)
        self.service.knowledge_mode = "enabled"
        unresolved = self.context_run(temporal_scope="historical")
        self.assertEqual(unresolved["data"]["packet"]["dictionary_claims"], [])
        explicit = {"applicable_at": "2025-12-01T12:00:00Z"}
        run = self.context_run(temporal_scope="historical", knowledge_query=explicit)
        self.assertEqual(len(run["data"]["packet"]["dictionary_claims"]), 1)
        self.context_run(knowledge_query={"applicable_at": "2025-12-01T12:00:00"}, status=422)
        self.set_time(fixtures.DUE)
        self.attempt(run, status=409)
        expired = self.context_run(temporal_scope="historical", knowledge_query=explicit)
        self.assertEqual(expired["data"]["packet"]["dictionary_claims"], [])
        self.assertEqual(expired["data"]["packet"]["knowledge_caveats"][0]["assessment"]["score"], .7)


if __name__ == "__main__":
    unittest.main()

"""K4 read-only resolution using fictional canonical history, never a live DB."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import test_knowledge_service as fixtures
from v02.knowledge_retrieval import KnowledgeRetrievalService
from v02.knowledge_retrieval_contracts import KnowledgeQueryContext, RETRIEVAL_POLICY
from v02.storage import Missing


class KnowledgeRetrievalTests(unittest.TestCase):
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
        clock = patch("v02.knowledge_retrieval.now", return_value="2026-01-01T10:15:00Z")
        self.query_clock = clock.start()
        self.addCleanup(clock.stop)
        self.resolver = KnowledgeRetrievalService(self.service)

    def proposition(self):
        value = fixtures.KnowledgeServiceTests.proposition(self)
        value.update(conditions=[], valid_from=None, valid_until=None)
        return value

    def adopted(self, *, proposition=None, source=None, sandbox=None):
        claim_id, source = self.create_claim(source, sandbox=sandbox, proposition=proposition)
        evidence = self.replace(claim_id, [self.evidence_spec(source)])
        search = self.search(claim_id)
        self.review(claim_id, evidence, search)
        return claim_id, source, evidence, search

    def select(self, *, principal=None, conversation="demo:chat", revision="r1", query="blue beacon", **values):
        with self.service.db.connection() as con:
            return self.resolver.select(con, principal or self.principal, conversation, revision, query, **values)

    def revalidate(self, manifest):
        with self.service.db.connection() as con:
            return self.resolver.revalidate_manifest(con, self.principal, manifest)

    def test_adopted_n_plus_one_resolves_review_plan_n_and_preserves_source_evidence(self):
        claim_id, source, evidence, search = self.adopted()
        before = deepcopy(self.claim(claim_id))
        counts = self.counts()
        result = self.select()
        self.assertEqual(len(result["dictionary_claims"]), 1, result["knowledge_caveats"])
        item = result["dictionary_claims"][0]
        self.assertEqual(item["claim_ref"], {"id": claim_id, "version": 3})
        self.assertEqual(item["target"]["evidence_set_revision"], 2)
        self.assertEqual(item["assessment"]["score"], .7)
        self.assertEqual(item["usage_scope"], "adopted_in_declared_scope")
        self.assertEqual(item["evidence_refs"], evidence)
        self.assertEqual(item["search_refs"], [search])
        self.assertEqual(item["source_material"][0]["source_ref"], source)
        self.assertEqual(item["source_material"][0]["origin_excerpt"], self.events[0]["content"])
        self.assertEqual(item["source_material"][0]["evidence_excerpts"][0]["excerpt"], self.events[0]["content"][13:37])
        self.assertTrue(self.revalidate(result["manifest"]))
        self.assertEqual(self.claim(claim_id), before)
        self.assertEqual(self.counts(), counts)

    def test_nonempty_conditions_remain_unknown_and_caveat_retains_claim_semantics(self):
        proposition = self.proposition()
        proposition["conditions"] = ["The demo operating condition holds"]
        claim_id, _, _, _ = self.adopted(proposition=proposition)
        result = self.select()
        self.assertEqual(result["dictionary_claims"], [])
        item = result["knowledge_caveats"][0]
        self.assertEqual(item["claim_ref"]["id"], claim_id)
        self.assertEqual(item["proposition"]["conditions"], proposition["conditions"])
        self.assertEqual(item["condition_checks"][0]["status"], "unknown")
        self.assertEqual(item["usage_scope"], "unadopted_do_not_assert_as_truth")
        self.assertIn("conditions_unknown", item["reasons"])
        with self.assertRaises(ValidationError):
            KnowledgeQueryContext.model_validate({"conditions_satisfied": True})

    def test_time_context_is_explicit_and_expiration_changes_freshness_not_truth(self):
        proposition = self.proposition()
        proposition.update(valid_from="2025-12-01T00:00:00Z", valid_until="2025-12-02T00:00:00Z")
        claim_id, _, _, _ = self.adopted(proposition=proposition)
        unknown = self.select(temporal_scope="historical")
        self.assertEqual(unknown["dictionary_claims"], [])
        self.assertIn("applicability_time_unresolved", unknown["knowledge_caveats"][0]["reasons"])
        explicit = self.select(temporal_scope="historical", applicable_at="2025-12-01T12:00:00Z")
        self.assertEqual(len(explicit["dictionary_claims"]), 1, explicit["knowledge_caveats"])
        self.query_clock.return_value = "2026-01-01T10:20:00Z"
        later = self.select(temporal_scope="historical", applicable_at="2025-12-01T12:00:00Z")
        self.assertEqual(later["freshness_digest"], explicit["freshness_digest"])
        self.query_clock.return_value = fixtures.DUE
        self.assertFalse(self.revalidate(explicit["manifest"]))
        self.assertEqual(self.claim(claim_id)["data"]["knowledge_state"], "established")
        self.assertEqual(self.claim(claim_id)["data"]["assessment"]["score"], .7)
        with self.assertRaises(ValidationError):
            KnowledgeQueryContext(applicable_at="2025-12-01T12:00:00")

    def test_next_turn_can_reuse_review_certificate_but_old_run_and_off_path_text_are_excluded(self):
        claim_id, _, _, _ = self.adopted()
        first = self.select()
        self.sync([{"id": "demo:followup", "role": "user", "content": "Tell me about the demo beacon"}],
                  self.active + ["demo:followup"], revision="r2", expected="r1")
        self.assertFalse(self.revalidate(first["manifest"]))
        current = self.select(revision="r2")
        self.assertEqual(len(current["dictionary_claims"]), 1, current["knowledge_caveats"])
        self.sync([], ["demo:tool"], revision="r3", expected="r2")
        off_path = self.select(revision="r3")
        self.assertEqual(off_path["dictionary_claims"], [])
        self.assertEqual(off_path["knowledge_caveats"], [])
        self.assertNotIn(claim_id, str(off_path))

    def test_new_plan_after_review_requires_fresh_review_even_without_claim_version_change(self):
        claim_id, _, _, _ = self.adopted()
        first = self.select()
        unchanged = deepcopy(self.claim(claim_id))
        request = self.claim_command(claim_id, queries=[{"purpose": purpose, "text": "demo absent finding",
            "reason": "Additional independent check of the fictional report"} for purpose in
            ("falsification", "alternative_explanation", "boundary_case", "source_correction")])
        self.service.countersearch.create_plan(self.identity, claim_id, request)
        second = self.select()
        self.assertEqual(second["dictionary_claims"], [])
        self.assertIn("new_information_requires_review", second["knowledge_caveats"][0]["reasons"])
        self.assertFalse(self.revalidate(first["manifest"]))
        self.assertEqual(self.claim(claim_id), unchanged)

    def test_new_search_inspection_is_detected_by_audit_order_when_timestamps_tie(self):
        claim_id, source = self.create_claim()
        evidence = self.replace(claim_id, [self.evidence_spec(source)])
        plan_req = self.claim_command(claim_id, queries=[{"purpose": purpose, "text": "blue",
            "reason": "Demo deliberately broad countersearch"} for purpose in
            ("falsification", "alternative_explanation", "boundary_case", "source_correction")])
        ack = self.service.countersearch.create_plan(self.identity, claim_id, plan_req)
        plan = self.ack_ref(ack, "knowledge_search_plan")
        ack = self.service.countersearch.execute(self.identity, plan["id"], self.command())
        search = self.ack_ref(ack, "knowledge_search")
        inspection = self.claim_command(claim_id, search_ref=search,
            results=[{"result_ref": source, "disposition": "irrelevant", "reason": "Demo hit is not counterevidence"}])
        self.service.knowledge_observations.record(self.identity, claim_id, inspection)
        self.review(claim_id, evidence, search)
        first = self.select()
        self.assertEqual(len(first["dictionary_claims"]), 1, first["knowledge_caveats"])
        inspection = self.claim_command(claim_id, search_ref=search,
            results=[{"result_ref": source, "disposition": "unresolved", "reason": "Demo new inspection finds an unresolved issue"}])
        self.service.knowledge_observations.record(self.identity, claim_id, inspection)
        result = self.select()
        self.assertEqual(result["dictionary_claims"], [])
        self.assertIn("new_information_requires_review", result["knowledge_caveats"][0]["reasons"])
        self.assertFalse(self.revalidate(first["manifest"]))

    def test_withdrawal_and_reopening_are_rechecked_without_implicit_state_changes(self):
        claim_id, _, evidence, search = self.adopted()
        first = self.select()
        _, withdrawal = self.review(claim_id, evidence, search, action="withdraw")
        self.assertFalse(self.revalidate(first["manifest"]))
        withdrawn = self.select()
        self.assertEqual(withdrawn["dictionary_claims"], [])
        self.assertEqual(withdrawn["knowledge_caveats"][0]["knowledge_state"], "withdrawal_candidate")
        search = self.search(claim_id)
        self.review(claim_id, evidence, search, reopens_withdrawal_ref=withdrawal)
        reopened = self.select()
        self.assertEqual(len(reopened["dictionary_claims"]), 1, reopened["knowledge_caveats"])

    def test_provisional_claims_do_not_become_knowledge_and_scopes_are_isolated(self):
        claim_id, _ = self.create_claim()
        normal = self.select()
        self.assertEqual(normal["dictionary_claims"], [])
        self.assertEqual(normal["knowledge_caveats"][0]["proposition"]["text"], self.proposition()["text"])
        self.assertIsNone(normal["knowledge_caveats"][0]["assessment"]["score"])
        self.assertEqual(self.select(context_key="other")["selected"], [])
        self.sync([{"id": "demo:other-conversation", "role": "user", "content": "Other demo"}],
                  ["demo:other-conversation"], conversation="demo:second-chat")
        self.assertEqual(self.select(conversation="demo:second-chat")["selected"], [])
        self.sync([{"id": "demo:other-owner-event", "role": "user", "content": "Other demo owner"}],
                  ["demo:other-owner-event"], principal="demo:other-owner")
        self.assertEqual(self.select(principal="demo:other-owner")["selected"], [])
        story_source = self.source("demo:fiction", sandbox="demo:story")
        story_id, _ = self.create_claim(story_source, sandbox="demo:story")
        self.assertEqual({item["claim_ref"]["id"] for item in self.select()["selected"]}, {claim_id})
        self.assertEqual({item["claim_ref"]["id"] for item in self.select(sandbox_id="demo:story")["selected"]}, {story_id})

    def test_old_review_without_k3_inspection_certificate_cannot_be_used_as_current_knowledge(self):
        claim_id, _, _, _ = self.adopted()
        ref = self.claim(claim_id)["data"]["latest_review_ref"]
        original = self.service.db.get

        def historical_k1(*args, **kwargs):
            record = original(*args, **kwargs)
            if record["id"] == ref["id"]:
                record = deepcopy(record)
                record["data"].pop("inspection")
            return record

        with patch.object(self.service.db, "get", side_effect=historical_k1):
            result = self.select()
        self.assertEqual(result["dictionary_claims"], [])
        self.assertIn("k3_adoption_certificate_required", result["knowledge_caveats"][0]["reasons"])

    def test_relevance_and_manifest_revalidation_detect_new_matches_without_raw_query_copy(self):
        claim_id, _, _, _ = self.adopted()
        first = self.select()
        self.assertNotIn("query", first["manifest"])
        self.assertEqual(first["manifest"]["query_terms"], ["beacon", "blue"])
        unrelated = self.proposition()
        unrelated["text"] = "The demo narrator reported a yellow garden."
        self.create_claim(proposition=unrelated)
        self.assertTrue(self.revalidate(first["manifest"]))
        self.create_claim()
        self.assertFalse(self.revalidate(first["manifest"]))
        self.assertEqual(self.select(query="unmatchedtoken")["selected"], [])
        self.assertEqual(self.claim(claim_id)["data"]["knowledge_state"], "established")

    def test_withdrawal_explanation_preserves_review_reason_and_original_report_as_history(self):
        claim_id, _, evidence, search = self.adopted()
        reason = "Demo reviewer withdrew adoption because the report was not independently checked."
        _, withdrawal = self.review(claim_id, evidence, search, action="withdraw", reason=reason)
        before, counts = deepcopy(self.claim(claim_id)), self.counts()
        result = self.select()
        self.assertEqual(result["dictionary_claims"], [])
        caveat = result["knowledge_caveats"][0]
        self.assertIn("withdrawal_candidate_do_not_adopt", caveat["reasons"])
        self.assertIn("withdrawal_not_reopened", caveat["reasons"])
        self.assertIn("historical records", caveat["state_explanation"])
        self.assertEqual(len(caveat["review_explanations"]), 1)
        review = caveat["review_explanations"][0]
        self.assertEqual(review["reference_roles"], ["latest_review", "withdrawal_ledger"])
        self.assertEqual(review["review_ref"], withdrawal)
        self.assertEqual(review["interpretation"], "historical_review_not_current_adoption")
        self.assertEqual(review["action"], "withdraw")
        self.assertEqual(review["provenance"]["reason"], reason)
        self.assertEqual(review["provenance"]["source"], "authorized_host:demo:host")
        self.assertEqual(review["provenance"]["created_at"], fixtures.STAMP)
        self.assertEqual(review["expected_claim_record_version"], 3)
        self.assertEqual(review["committed_claim_record_version"], 4)
        self.assertTrue(review["matches_current_target"])
        self.assertEqual(review["reviewed_proposition"], self.proposition())
        self.assertEqual(review["reviewed_evidence"][0]["excerpt"], self.events[0]["content"][13:37])
        self.assertEqual(caveat["source_material"][0]["origin_excerpt"], self.events[0]["content"])
        self.assertEqual(result["manifest"]["policy_version"], RETRIEVAL_POLICY)
        self.assertEqual(RETRIEVAL_POLICY, "knowledge.retrieval.k4.v2")
        self.assertTrue(any(ref["kind"] == "knowledge_claim_version"
                            for ref in result["manifest"]["selected"][0]["dependencies"]))
        old_policy = deepcopy(result["manifest"])
        old_policy["policy_version"] = "knowledge.retrieval.k4.v1"
        self.assertFalse(self.revalidate(old_policy))
        self.assertTrue(self.revalidate(result["manifest"]))
        self.assertEqual(self.claim(claim_id), before)
        self.assertEqual(self.counts(), counts)

    def test_hold_keeps_separate_withdrawal_reason_and_reopen_does_not_erase_history(self):
        claim_id, _, evidence, search = self.adopted()
        withdrawal_reason = "Demo original adoption was explicitly withdrawn."
        _, withdrawal = self.review(claim_id, evidence, search, action="withdraw", reason=withdrawal_reason)
        _, hold = self.review(claim_id, evidence, search, action="hold", reason="Demo awaiting another inspection.")
        held = self.select()
        caveat = held["knowledge_caveats"][0]
        self.assertIn("provisional_pending_review", caveat["reasons"])
        self.assertIn("withdrawal_not_reopened", caveat["reasons"])
        explanations = {item["review_ref"]["id"]: item for item in caveat["review_explanations"]}
        self.assertEqual(explanations[hold["id"]]["reference_roles"], ["latest_review"])
        self.assertEqual(explanations[withdrawal["id"]]["reference_roles"], ["withdrawal_ledger"])
        self.assertEqual(explanations[withdrawal["id"]]["provenance"]["reason"], withdrawal_reason)
        self.assertIn("does not cancel", caveat["state_explanation"])
        search = self.search(claim_id)
        self.review(claim_id, evidence, search, reopens_withdrawal_ref=withdrawal)
        reopened = self.select()
        adopted = reopened["dictionary_claims"][0]
        self.assertEqual(len(adopted["review_explanations"]), 2)
        self.assertNotIn("withdrawal_not_reopened", adopted["reasons"])
        self.assertEqual(adopted["review_explanations"][0]["reopens_withdrawal_ref"], withdrawal)
        self.assertFalse(self.revalidate(held["manifest"]))

    def test_old_target_withdrawal_explains_history_without_certifying_edited_claim(self):
        claim_id, _, evidence, search = self.adopted()
        self.review(claim_id, evidence, search, action="withdraw", reason="Demo withdrawal concerns the earlier blue claim.")
        changed = self.proposition()
        changed.update(text="The demo narrator reported a red beacon.", object="red")
        self.service.knowledge.edit_claim(self.identity, claim_id, self.claim_command(claim_id, proposition=changed))
        before = deepcopy(self.claim(claim_id))
        result = self.select(query="red beacon")
        self.assertEqual(result["dictionary_claims"], [])
        caveat = result["knowledge_caveats"][0]
        historical = caveat["review_explanations"][0]
        self.assertEqual(caveat["proposition"]["object"], "red")
        self.assertEqual(historical["reviewed_proposition"]["object"], "blue")
        self.assertFalse(historical["matches_current_target"])
        self.assertLess(historical["target"]["claim_revision"], caveat["target"]["claim_revision"])
        self.assertLess(historical["committed_claim_record_version"], caveat["claim_ref"]["version"])
        self.assertEqual(historical["interpretation"], "historical_review_not_current_adoption")
        self.assertIsNone(caveat["review_ref"])
        self.assertIsNone(caveat["assessment"]["score"])
        self.assertIn("withdrawal_not_reopened", caveat["reasons"])
        self.assertEqual(self.claim(claim_id), before)

    def test_dispute_explanation_is_distinct_from_a_candidate_without_review(self):
        claim_id, _, evidence, search = self.adopted()
        reason = "Demo reviewer disputes the scope of the reported observation."
        self.review(claim_id, evidence, search, action="dispute", reason=reason)
        candidate_id, _ = self.create_claim()
        caveats = {item["claim_ref"]["id"]: item for item in self.select()["knowledge_caveats"]}
        self.assertIn("disputed_requires_resolution", caveats[claim_id]["reasons"])
        self.assertEqual(caveats[claim_id]["review_explanations"][0]["provenance"]["reason"], reason)
        self.assertIn("unresolved dispute", caveats[claim_id]["state_explanation"])
        self.assertIn("provisional_pending_review", caveats[candidate_id]["reasons"])
        self.assertEqual(caveats[candidate_id]["review_explanations"], [])

    def test_off_path_review_basis_hides_reason_and_evidence_without_losing_withdrawal_state(self):
        claim_id, source, _, _ = self.adopted()
        tool = self.source("demo:tool")
        evidence = self.replace(claim_id, [self.evidence_spec(source), self.evidence_spec(tool, relation="qualifies",
            span_start=0, span_end=len(self.events[1]["content"]))])
        self.review(claim_id, evidence, None, action="withdraw", counterevidence_status="potential",
                    counterevidence_refs=[evidence[1]], reason="demo:off-path-review-reason")
        before = self.select()
        self.assertEqual(len(before["knowledge_caveats"][0]["review_explanations"]), 1)
        self.sync([], ["demo:user"], revision="r2", expected="r1")
        result = self.select(revision="r2")
        self.assertEqual(result["dictionary_claims"], [])
        caveat = result["knowledge_caveats"][0]
        self.assertEqual(caveat["knowledge_state"], "withdrawal_candidate")
        self.assertEqual(caveat["review_explanations"], [])
        self.assertIn("review_explanation_unavailable", caveat["reasons"])
        self.assertIn("do not infer", caveat["state_explanation"])
        self.assertNotIn("demo:off-path-review-reason", str(result))
        self.assertNotIn(self.events[1]["content"], str(result))
        self.assertFalse(self.revalidate(before["manifest"]))

    def test_unresolved_or_wrong_scope_review_explanation_fails_closed_without_reason_leak(self):
        claim_id, _, evidence, search = self.adopted()
        self.review(claim_id, evidence, search, action="withdraw", reason="demo:must-not-leak-invalid-review")
        review_ref = self.claim(claim_id)["data"]["latest_review_ref"]
        original = self.service.db.get
        baseline = self.select()
        for corruption in ("missing", "scope", "commit"):
            with self.subTest(corruption=corruption):
                def unavailable(*args, **kwargs):
                    record = original(*args, **kwargs)
                    if record["id"] != review_ref["id"]:
                        return record
                    if corruption == "missing":
                        raise Missing("demo missing review")
                    record = deepcopy(record)
                    if corruption == "scope":
                        record["data"]["snapshot"]["scope"]["conversation_id"] = "demo:unrelated-chat"
                    else:
                        record["data"]["committed_record_version"] += 1
                    return record

                with patch.object(self.service.db, "get", side_effect=unavailable):
                    result = self.select()
                    self.assertFalse(self.revalidate(baseline["manifest"]))
                self.assertEqual(result["dictionary_claims"], [])
                self.assertEqual(result["knowledge_caveats"][0]["review_explanations"], [])
                self.assertIn("review_explanation_unavailable", result["knowledge_caveats"][0]["reasons"])
                self.assertNotIn("demo:must-not-leak-invalid-review", str(result))


if __name__ == "__main__":
    unittest.main()

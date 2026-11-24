"""K3 inspection gates with fictional sources and isolated temporary databases."""
from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_knowledge_service as fixtures


KNOWLEDGE = fixtures.KNOWLEDGE


class KnowledgeObservationTests(unittest.TestCase):
    # Share setup helpers, without inheriting and rerunning the K1 test suite.
    setUp = fixtures.KnowledgeServiceTests.setUp
    remove_fixture = fixtures.KnowledgeServiceTests.remove_fixture
    call = fixtures.KnowledgeServiceTests.call
    sync = fixtures.KnowledgeServiceTests.sync
    command = fixtures.KnowledgeServiceTests.command
    ack_ref = fixtures.KnowledgeServiceTests.ack_ref
    source = fixtures.KnowledgeServiceTests.source
    proposition = fixtures.KnowledgeServiceTests.proposition
    create_claim = fixtures.KnowledgeServiceTests.create_claim
    claim = fixtures.KnowledgeServiceTests.claim
    claim_command = fixtures.KnowledgeServiceTests.claim_command
    evidence_spec = fixtures.KnowledgeServiceTests.evidence_spec
    replace = fixtures.KnowledgeServiceTests.replace
    search = fixtures.KnowledgeServiceTests.search
    review_request = fixtures.KnowledgeServiceTests.review_request
    review = fixtures.KnowledgeServiceTests.review
    prepared = fixtures.KnowledgeServiceTests.prepared
    counts = fixtures.KnowledgeServiceTests.counts

    def run_search(self, claim, term, *, revision="r1", execute=True):
        queries = [{"purpose": purpose, "text": "Fictional corpus inspection: " + term,
                    "terms": [term], "reason": "Check this possible objection in the demo corpus"}
                   for purpose in ("falsification", "alternative_explanation", "boundary_case", "source_correction")]
        result = self.call("POST", KNOWLEDGE + f"/claims/{claim}/search-plans",
            body=self.claim_command(claim, revision=revision, queries=queries))
        plan = self.ack_ref(result, "knowledge_search_plan")
        if not execute:
            return plan
        return self.call("POST", KNOWLEDGE + f"/search-plans/{plan['id']}/execute",
                         body=self.command(revision=revision))

    @staticmethod
    def result(ref, disposition="irrelevant", evidence=(), **changes):
        return {"result_ref": ref, "disposition": disposition,
                "reason": "Authorized inspection of this fictional finding",
                "evidence_refs": list(evidence), **changes}

    @staticmethod
    def pair(left, right, status="independent"):
        return {"evidence_ids": [left["id"], right["id"]], "status": status,
                "reason": "Fictional original-source provenance inspection"}

    def observe(self, claim, *, search=None, attempt=None, results=(), dependencies=(), revision="r1",
                status=200, token="demo-reviewer"):
        request = self.claim_command(claim, revision=revision, search_ref=search, attempt_ref=attempt,
                                     results=list(results), dependencies=list(dependencies))
        response = self.call("POST", KNOWLEDGE + f"/claims/{claim}/search-observations",
                             body=request, status=status, token=token)
        return self.ack_ref(response, "knowledge_search_observation") if status == 200 else response

    def rejected_adoption(self, claim, evidence, search, *, reason, **changes):
        before, counts = deepcopy(self.claim(claim)), self.counts()
        request = self.review_request(claim, evidence, search, **changes)
        result = self.call("POST", KNOWLEDGE + f"/claims/{claim}/reviews", body=request, status=422)
        self.assertIn(reason, str(result))
        self.assertEqual(self.claim(claim), before)
        self.assertEqual(self.counts(), counts)

    def empirical(self):
        value = self.proposition()
        value.update(text="The demo beacon was blue in the bounded observation.", kind="empirical",
                     subject="demo:beacon", predicate="observed_colour", quantifier="bounded",
                     attribution_actor_id=None)
        return value

    def test_actual_hit_must_be_inspected_before_adoption(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        result = self.run_search(claim, "prefix")
        search = self.ack_ref(result, "knowledge_search")
        stored = self.service.get(self.principal, search["id"])
        self.assertEqual(stored["data"]["snapshot"]["result_refs"], [source])
        self.rejected_adoption(claim, evidence, search, reason="search_result_unresolved")
        observation = self.observe(claim, search=search,
            results=[self.result(source, "evidence_linked", evidence)])
        _, review = self.review(claim, evidence, search)
        inspection = self.service.get(self.principal, review["id"])["data"]["inspection"]
        self.assertEqual(inspection["observation_refs"], [observation])
        self.assertEqual(inspection["obligation_refs"], [source])
        self.assertEqual(inspection["unresolved"], [])

    def test_manual_completed_empty_search_does_not_establish_execution(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        request = self.claim_command(claim, status="completed", search_scope="Demo local corpus",
            planned_queries=["Demo correction"], executed_queries=["Demo correction"], result_refs=[],
            executor_version="manual.demo.v1", coverage="Manual assertion of zero results", limitations=[])
        result = self.call("POST", KNOWLEDGE + f"/claims/{claim}/searches", body=request)
        search = self.ack_ref(result, "knowledge_search")
        self.rejected_adoption(claim, evidence, search, reason="actual_countersearch_required")

    def test_omitting_an_inconvenient_search_does_not_hide_its_results(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        hit = self.ack_ref(self.run_search(claim, "prefix"), "knowledge_search")
        empty = self.search(claim)
        self.rejected_adoption(claim, evidence, empty, reason="search_result_unresolved")
        self.observe(claim, search=hit, results=[self.result(source, "evidence_linked", evidence)])
        self.review(claim, evidence, empty)

    def test_observation_requires_exact_result_coverage_and_canonical_versions(self):
        claim, first = self.create_claim()
        second = self.source("demo:tool")
        evidence = self.replace(claim, [self.evidence_spec(first)])
        search = self.ack_ref(self.run_search(claim, "blue"), "knowledge_search")
        before = self.counts()
        self.observe(claim, search=search, results=[self.result(first)], status=422)
        self.observe(claim, search=search,
            results=[self.result(first), self.result({**second, "version": 2})], status=422)
        self.assertEqual(self.counts(), before)
        self.observe(claim, search=search,
            results=[self.result(first, "evidence_linked", evidence), self.result(second)])
        self.review(claim, evidence, search)

    def test_evidence_replacement_retains_old_search_and_counterevidence_obligations(self):
        claim, first = self.create_claim()
        second = self.source("demo:tool")
        evidence = self.replace(claim, [self.evidence_spec(first), self.evidence_spec(second,
            relation="refutes", span_start=0, span_end=20,
            review_reason="Fictional reviewer records this instrument finding as a possible objection")])
        old_search = self.ack_ref(self.run_search(claim, "independent"), "knowledge_search")
        self.observe(claim, search=old_search, results=[self.result(second, "evidence_linked", evidence[1:])])
        current = self.replace(claim, [self.evidence_spec(first)])
        fresh_search = self.search(claim)
        self.rejected_adoption(claim, current, fresh_search, reason="search_result_unresolved")
        # Rechecking only the old search does not hide its separately recorded objection.
        self.observe(claim, search=old_search, results=[self.result(second)])
        self.rejected_adoption(claim, current, fresh_search, reason=evidence[1]["id"])
        self.observe(claim, results=[self.result(evidence[1])])
        self.review(claim, current, fresh_search)
        self.assertEqual(self.service.get(self.principal, evidence[1]["id"])["data"]["snapshot"]["relation"], "refutes")

    def test_historical_counterevidence_without_a_search_still_requires_inspection(self):
        claim, first = self.create_claim()
        second = self.source("demo:tool")
        old = self.replace(claim, [self.evidence_spec(first), self.evidence_spec(second,
            relation="qualifies", content_check="unreviewed", span_start=0, span_end=20)])
        current = self.replace(claim, [self.evidence_spec(first)])
        search = self.search(claim)
        self.rejected_adoption(claim, current, search, reason=old[1]["id"])
        self.observe(claim, results=[self.result(old[1])])
        self.review(claim, current, search)

    def test_same_timestamp_latest_audit_observation_controls_result_resolution(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        search = self.ack_ref(self.run_search(claim, "prefix"), "knowledge_search")
        first = self.observe(claim, search=search, results=[self.result(source, "evidence_linked", evidence)])
        second = self.observe(claim, search=search, results=[self.result(source, "unresolved")])
        stamps = [self.service.get(self.principal, ref["id"])["data"]["snapshot"]["provenance"]["created_at"]
                  for ref in (first, second)]
        self.assertEqual(stamps, [fixtures.STAMP, fixtures.STAMP])
        self.rejected_adoption(claim, evidence, search, reason="search_result_unresolved")
        self.observe(claim, search=search, results=[self.result(source, "evidence_linked", evidence)])
        self.review(claim, evidence, search)

    def test_removed_intermediate_source_does_not_break_hidden_dependency_chain(self):
        first, middle, last = self.source(), self.source("demo:assistant"), self.source("demo:tool")
        claim, _ = self.create_claim(first, proposition=self.empirical())
        specs = [self.evidence_spec(first, source_kind="direct_observation"),
                 self.evidence_spec(middle, source_kind="model_output", relation="derived_from"),
                 self.evidence_spec(last, source_kind="direct_observation", span_start=0, span_end=20)]
        old = self.replace(claim, specs)
        self.observe(claim, dependencies=[
            {"pair": self.pair(old[0], old[1], "dependent"), "method": "shared_lineage"},
            {"pair": self.pair(old[1], old[2], "dependent"), "method": "shared_lineage"}])
        current = self.replace(claim, [specs[0], specs[2]])
        pair = self.pair(*current)
        self.observe(claim, dependencies=[{"pair": pair, "method": "original_provenance_inspection",
                                          "verification_refs": [first, last]}])
        search = self.search(claim)
        self.rejected_adoption(claim, current, search, reason="independence_dependency_conflict",
            profile="independent_observations", source_independence="independent", independence_pairs=[pair])

    def test_unknown_and_model_only_verification_cannot_certify_independence(self):
        first, second, model = self.source(), self.source("demo:tool"), self.source("demo:assistant")
        claim, _ = self.create_claim(first, proposition=self.empirical())
        evidence = self.replace(claim, [self.evidence_spec(first, source_kind="direct_observation"),
            self.evidence_spec(second, source_kind="direct_observation", span_start=0, span_end=20)])
        independent = self.pair(*evidence)
        self.observe(claim, dependencies=[{"pair": self.pair(*evidence, status="unknown"), "method": "unresolved"}])
        before = self.counts()
        self.observe(claim, dependencies=[{"pair": independent, "method": "original_provenance_inspection",
                                          "verification_refs": [model]}], status=422)
        self.assertEqual(self.counts(), before)
        wrapped_model = self.source("demo:tool", parents=[model])
        before = self.counts()
        rejected = self.observe(claim, dependencies=[{"pair": independent,
            "method": "original_provenance_inspection", "verification_refs": [wrapped_model]}], status=422)
        self.assertIn("Known model origins", str(rejected))
        self.assertEqual(self.counts(), before)
        search = self.search(claim)
        self.rejected_adoption(claim, evidence, search, reason="independence_inspection_missing",
            profile="independent_observations", source_independence="independent", independence_pairs=[independent])
        self.observe(claim, dependencies=[{"pair": independent, "method": "original_provenance_inspection",
                                          "verification_refs": [first, second]}])
        self.review(claim, evidence, search, profile="independent_observations",
                    source_independence="independent", independence_pairs=[independent])

    def test_source_reliability_does_not_change_world_confidence_or_claim_record(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        search = self.ack_ref(self.run_search(claim, "prefix"), "knowledge_search")
        before = deepcopy(self.claim(claim))
        reliability = {"target": "source_reliability", "status": "assessed", "score": 1.0,
                       "evidence_refs": [source["id"]]}
        observation = self.observe(claim, search=search,
            results=[self.result(source, "evidence_linked", evidence, source_reliability=reliability)])
        self.assertEqual(self.claim(claim), before)
        self.assertIsNone(self.claim(claim)["data"]["assessment"]["score"])
        self.review(claim, evidence, search)
        self.assertEqual(self.claim(claim)["data"]["assessment"]["score"], .7)
        inspected = self.call("GET", KNOWLEDGE + "/search-observations/" + observation["id"], token="demo-reader")
        self.assertEqual(inspected["data"]["snapshot"]["results"][0]["source_reliability"]["score"], 1.0)
        self.assertEqual(inspected["current_applicability"], "not_evaluated")

    def test_observation_review_authority_owner_and_revision_are_required(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        search = self.ack_ref(self.run_search(claim, "prefix"), "knowledge_search")
        results = [self.result(source, "evidence_linked", evidence)]
        self.observe(claim, search=search, results=results, token="demo-writer", status=403)
        self.observe(claim, search=search, results=results, token="demo-other-owner", status=404)
        stale = self.claim_command(claim, search_ref=search, results=results)
        self.replace(claim, [self.evidence_spec(source, span_start=0, span_end=12)])
        self.call("POST", KNOWLEDGE + f"/claims/{claim}/search-observations", body=stale, status=409)
        observation = self.observe(claim, search=search, results=[self.result(source)])
        self.call("GET", KNOWLEDGE + "/search-observations/" + observation["id"],
                  token="demo-other-owner", status=404)

    def test_interrupted_attempt_results_remain_obligations_after_new_path_search(self):
        claim, source = self.create_claim()
        evidence = self.replace(claim, [self.evidence_spec(source)])
        plan = self.run_search(claim, "prefix", execute=False)
        original = self.service.countersearch._run

        def run_then_change_path(*args):
            result = original(*args)
            self.sync([], self.active, revision="r2", expected="r1")
            return result

        with patch.object(self.service.countersearch, "_run", side_effect=run_then_change_path):
            result = self.call("POST", KNOWLEDGE + f"/search-plans/{plan['id']}/execute", body=self.command())
        attempt = self.ack_ref(result, "knowledge_search_attempt")
        self.assertNotIn("knowledge_search", [record["kind"] for record in result["records"]])
        stored = self.call("GET", KNOWLEDGE + "/search-attempts/" + attempt["id"])
        self.assertEqual(stored["data"]["status"], "blocked")
        self.assertEqual(stored["data"]["result_refs"], [source])
        search = self.ack_ref(self.run_search(claim, "no-demo-hit", revision="r2"), "knowledge_search")
        self.rejected_adoption(claim, evidence, search, revision="r2", reason="search_result_unresolved")
        self.observe(claim, attempt=attempt, revision="r2",
            results=[self.result(source, "evidence_linked", evidence)])
        self.review(claim, evidence, search, revision="r2")


if __name__ == "__main__":
    unittest.main()

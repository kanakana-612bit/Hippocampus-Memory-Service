"""Fictional K0 examples; no DB, API, model, or deployment state is accessed."""
from __future__ import annotations

from copy import deepcopy
import unittest

from pydantic import ValidationError

from v02.knowledge_contracts import (
    AdoptionContext,
    ClaimSnapshot,
    CounterevidenceReview,
    CounterevidenceSearch,
    KnowledgeCandidate,
    KnowledgeReview,
    KnowledgeScope,
    Proposition,
    adoption_decision,
    initial_claim,
)


CREATED = "2026-01-01T10:00:00Z"
SEARCHED = "2026-01-01T10:05:00Z"
REVIEWED = "2026-01-01T10:10:00Z"
EVALUATED = "2026-01-01T10:15:00Z"
DUE = "2026-01-01T11:00:00Z"


def demo_provenance(target="review", created_at=CREATED):
    return {
        "source": "demo:reviewer",
        "reason": "Fictional contract example, not an empirical claim",
        "confidence": None,
        "confidence_target": target,
        "created_at": created_at,
        "decay": {"kind": "none", "reason": "historical_demo_record"},
        "expiry": None,
        "policy_version": "knowledge.k0.v1",
        "cause_refs": [],
    }


def demo_candidate():
    return {
        "id": "demo:claim",
        "scope": {
            "principal_id": "demo:principal",
            "conversation_id": "demo:conversation",
            "context_key": "demo:context",
            "domain": "world",
            "sandbox_id": None,
        },
        "proposition": {
            "text": "The demo beacon was blue during the recorded observation.",
            "kind": "empirical",
            "subject": "demo:beacon",
            "predicate": "has_colour",
            "object": "blue",
            "polarity": "affirmed",
            "quantifier": "bounded",
            "conditions": ["recorded demo observation"],
            "valid_from": "2026-01-01T09:00:00Z",
            "valid_until": "2026-01-01T12:00:00Z",
        },
        "origin_refs": [{"id": "demo:episode", "version": 1}],
        "provenance": demo_provenance("candidate_origin"),
    }


def demo_adopted(profile="independent_observations"):
    """Build declared review assertions, not proof or authorization machinery."""
    candidate = demo_candidate()
    proposition = candidate["proposition"]
    kind, quantifier, source_kind = {
        "independent_observations": ("empirical", "bounded", "direct_observation"),
        "attributed_report": ("attributed_report", "single", "primary_report"),
        "direct_observation": ("bounded_observation", "single", "direct_observation"),
        "formal_derivation": ("formal", "universal", "checked_derivation"),
    }[profile]
    proposition.update(kind=kind, quantifier=quantifier)
    if profile == "attributed_report":
        proposition.update(
            text="The demo narrator reported that the beacon was blue.",
            predicate="reported_colour",
            attribution_actor_id="demo:narrator",
        )
    elif profile == "formal_derivation":
        proposition.update(
            text="Every demo beacon in set B belongs to set A under the supplied axioms.",
            predicate="is_member_of",
            object="demo:set-A",
            conditions=["demo axiom: B is a subset of A"],
        )
    claim = initial_claim(KnowledgeCandidate.model_validate(candidate)).model_dump()
    target = {"claim_id": claim["id"], "claim_revision": 1, "evidence_set_revision": 1}
    evidence = []
    count = 2 if profile == "independent_observations" else 1
    for index in range(count):
        evidence.append({
            "ref": {"id": f"demo:evidence-{index}", "version": 1},
            "source_ref": {"id": f"demo:source-{index}", "version": 1},
            "scope": deepcopy(claim["scope"]),
            "target": deepcopy(target),
            "relation": "supports",
            "content_check": "matched",
            "source_kind": source_kind,
            "origin_ids": [f"demo:root-observation-{index}"],
            "origin_status": "known",
            "source_actor_id": "demo:narrator" if profile == "attributed_report" else None,
            "source_digest": str(index + 1) * 64,
            "excerpt": "Fictional matching evidence under this profile.",
            "verification_refs": [{"id": "demo:derivation-check", "version": 1}]
            if profile == "formal_derivation" else [],
            "provenance": demo_provenance("content_match"),
        })
    independence = {
        "status": "independent" if count == 2 else "unknown",
        "pairs": [{"evidence_ids": ["demo:evidence-0", "demo:evidence-1"],
                   "status": "independent", "reason": "Two separately observed demo roots"}]
        if count == 2 else [],
        "provenance": demo_provenance("source_independence"),
    }
    search = {
        "ref": {"id": "demo:search", "version": 1},
        "target": deepcopy(target),
        "scope": deepcopy(claim["scope"]),
        "status": "completed",
        "search_scope": "Demo fixture corpus only",
        "planned_queries": ["Find demo observations incompatible with the proposition"],
        "executed_queries": ["Find demo observations incompatible with the proposition"],
        "result_refs": [],
        "executor_version": "demo:executor-v1",
        "coverage": "All entries in the fictional two-observation corpus",
        "limitations": ["No claim about unobserved times or external sources"],
        "completed_at": SEARCHED,
        "provenance": demo_provenance("search_execution"),
    }
    review = {
        "ref": {"id": "demo:review", "version": 1},
        "target": target,
        "scope": deepcopy(claim["scope"]),
        "action": "adopt",
        "state_after": "established",
        "profile": profile,
        "assessment": {
            "target": "world_claim", "claim": proposition["text"], "score": 0.7,
            "status": "assessed", "calibrated": False,
            "evidence_refs": [item["ref"]["id"] for item in evidence],
            "policy_version": "evidence-only.v0.2",
        },
        "evidence": evidence,
        "counterevidence": {
            "status": "none_found_in_scope", "searches": [search], "evidence_refs": [],
            "provenance": demo_provenance("counterevidence_review", SEARCHED),
        },
        "source_independence": independence,
        "review_after": DUE,
        "provenance": demo_provenance("adoption_review", REVIEWED),
    }
    claim.update(
        knowledge_state="established", assessment=deepcopy(review["assessment"]),
        counterevidence_status="none_found_in_scope", source_independence=independence["status"],
        latest_review_ref=deepcopy(review["ref"]), review_after=DUE,
    )
    context = {
        "scope": deepcopy(claim["scope"]), "evaluated_at": EVALUATED, "applicable_at": EVALUATED,
        "review_authorized": True, "path_eligible": True, "references_resolvable": True,
        "conditions_satisfied": True,
    }
    return claim, review, context


def decide(claim, review, context):
    return adoption_decision(
        ClaimSnapshot.model_validate(claim), KnowledgeReview.model_validate(review),
        AdoptionContext.model_validate(context),
    )


def counterexample(review, *, relation="refutes", content_check="matched"):
    item = deepcopy(review["evidence"][0])
    item.update(
        ref={"id": "demo:counterevidence", "version": 1},
        source_ref={"id": "demo:counter-source", "version": 1},
        origin_ids=["demo:counter-root"], relation=relation, content_check=content_check,
        excerpt="A conflicting fictional observation requiring review.",
    )
    review["evidence"].append(item)
    return item


class CandidateContractTests(unittest.TestCase):
    def test_ordinary_candidate_cannot_assert_truth_state_or_weights(self):
        injected = {
            "knowledge_state": "established", "assessment": {"score": 1.0},
            "confidence": 1.0, "retrieval_weight": 1.0, "generalization_weight": 1.0,
            "salience": 1.0, "epistemic_status": "confirmed",
            "source_independence": "independent", "counterevidence_status": "none_found_in_scope",
            "emotional_confidence": 1.0,
        }
        for key, value in injected.items():
            with self.subTest(field=key), self.assertRaises(ValidationError):
                KnowledgeCandidate.model_validate({**demo_candidate(), key: value})

    def test_initial_candidate_is_provisional_with_unknown_world_confidence(self):
        candidate = KnowledgeCandidate.model_validate(demo_candidate())
        before = deepcopy(candidate.model_dump())
        claim = initial_claim(candidate)
        self.assertEqual(claim.knowledge_state, "provisional")
        self.assertEqual(claim.assessment.target, "world_claim")
        self.assertEqual(claim.assessment.claim, candidate.proposition.text)
        self.assertEqual(claim.assessment.status, "unknown")
        self.assertIsNone(claim.assessment.score)
        self.assertEqual(claim.counterevidence_status, "unassessed")
        self.assertEqual(claim.source_independence, "unknown")
        self.assertIsNone(claim.latest_review_ref)
        self.assertEqual(candidate.model_dump(), before)

    def test_exact_fingerprint_preserves_meaning_and_scope_axes(self):
        candidate = KnowledgeCandidate.model_validate(demo_candidate())
        original = candidate.proposition.exact_fingerprint(candidate.scope)
        proposition_changes = {
            "text": "A paraphrase must not merge automatically.",
            "kind": "bounded_observation", "subject": "demo:other-beacon",
            "predicate": "appears_colour", "object": "red", "polarity": "negated",
            "quantifier": "universal", "attribution_actor_id": "demo:other-narrator",
            "conditions": ["a different demo condition"],
            "valid_from": "2026-01-01T08:00:00Z", "valid_until": "2026-01-01T13:00:00Z",
        }
        for key, value in proposition_changes.items():
            with self.subTest(proposition_axis=key):
                changed = Proposition.model_validate({**candidate.proposition.model_dump(), key: value})
                self.assertNotEqual(original, changed.exact_fingerprint(candidate.scope))
        for key in ("principal_id", "conversation_id", "context_key"):
            with self.subTest(scope_axis=key):
                scope = KnowledgeScope.model_validate({**candidate.scope.model_dump(), key: "demo:other"})
                self.assertNotEqual(original, candidate.proposition.exact_fingerprint(scope))
        fictional = KnowledgeScope.model_validate({
            **candidate.scope.model_dump(), "domain": "fiction", "sandbox_id": "demo:sandbox-A"})
        other_fiction = KnowledgeScope.model_validate({**fictional.model_dump(), "sandbox_id": "demo:sandbox-B"})
        self.assertNotEqual(original, candidate.proposition.exact_fingerprint(fictional))
        self.assertNotEqual(candidate.proposition.exact_fingerprint(fictional),
                            candidate.proposition.exact_fingerprint(other_fiction))

    def test_duplicate_origins_and_expiring_history_are_rejected(self):
        candidate = demo_candidate()
        candidate["origin_refs"].append({"id": "demo:episode", "version": 2})
        with self.assertRaises(ValidationError):
            KnowledgeCandidate.model_validate(candidate)
        for change in ({"expiry": DUE}, {"decay": {"kind": "exponential", "reason": "demo"}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                candidate = demo_candidate()
                candidate["provenance"].update(change)
                KnowledgeCandidate.model_validate(candidate)


class AdoptionContractTests(unittest.TestCase):
    def test_four_explicit_evidence_profiles_can_be_adopted(self):
        for profile in ("independent_observations", "attributed_report", "direct_observation", "formal_derivation"):
            with self.subTest(profile=profile):
                result = decide(*demo_adopted(profile))
                self.assertTrue(result.eligible, result.reasons)

    def test_repetition_shared_origin_and_unknown_origin_are_not_independence(self):
        for change in ("shared_origin", "same_source", "unknown_origin"):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                _, review, _ = demo_adopted()
                first, second = review["evidence"]
                if change == "shared_origin":
                    second["origin_ids"] = first["origin_ids"][:]
                elif change == "same_source":
                    second["source_ref"] = deepcopy(first["source_ref"])
                else:
                    second.update(origin_status="unknown", origin_ids=[])
                KnowledgeReview.model_validate(review)

    def test_independent_claim_needs_primary_support_not_multiple_model_outputs(self):
        claim, review, context = demo_adopted()
        for item in review["evidence"]:
            item["source_kind"] = "model_output"
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("independent_support_missing", result.reasons)

    def test_profile_cannot_expand_a_report_or_observation_into_universal_truth(self):
        for profile in ("attributed_report", "direct_observation", "independent_observations"):
            with self.subTest(profile=profile):
                claim, review, context = demo_adopted(profile)
                claim["proposition"]["quantifier"] = "universal"
                result = decide(claim, review, context)
                self.assertFalse(result.eligible)
                self.assertIn("profile_scope_mismatch", result.reasons)

    def test_each_special_profile_requires_its_own_evidence(self):
        for profile, field, value, reason in (
            ("attributed_report", "source_actor_id", "demo:wrong-speaker", "attributed_primary_report_missing"),
            ("direct_observation", "origin_status", "unknown", "direct_observation_missing"),
            ("formal_derivation", "verification_refs", [], "checked_derivation_missing"),
        ):
            with self.subTest(profile=profile):
                claim, review, context = demo_adopted(profile)
                review["evidence"][0][field] = value
                result = decide(claim, review, context)
                self.assertFalse(result.eligible)
                self.assertIn(reason, result.reasons)

    def test_old_claim_or_evidence_set_review_is_rejected_but_record_version_is_not_semantic(self):
        for field in ("claim_revision", "evidence_set_revision"):
            with self.subTest(revision=field):
                claim, review, context = demo_adopted()
                claim[field] = 2
                result = decide(claim, review, context)
                self.assertFalse(result.eligible)
                self.assertIn("stale_review_target", result.reasons)
        claim, review, context = demo_adopted()
        claim["record_version"] = 7
        self.assertTrue(decide(claim, review, context).eligible)

    def test_review_rejects_evidence_for_another_claim_or_scope(self):
        for change in ("claim", "scope"):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                _, review, _ = demo_adopted()
                if change == "claim":
                    review["evidence"][0]["target"]["claim_revision"] = 2
                else:
                    review["evidence"][0]["scope"]["principal_id"] = "demo:other"
                KnowledgeReview.model_validate(review)

    def test_stale_counterevidence_reference_is_rejected(self):
        _, review, _ = demo_adopted()
        item = counterexample(review)
        review["counterevidence"].update(status="material_unresolved", evidence_refs=[deepcopy(item["ref"])])
        item["ref"]["version"] = 2
        with self.assertRaises(ValidationError):
            KnowledgeReview.model_validate(review)

    def test_report_and_source_confidence_cannot_substitute_for_world_confidence(self):
        for target in ("report_fidelity", "source_reliability"):
            with self.subTest(target=target), self.assertRaises(ValidationError):
                _, review, _ = demo_adopted()
                review["assessment"].update(target=target, score=1.0)
                KnowledgeReview.model_validate(review)
        claim, review, context = demo_adopted()
        for confidence in (0.0, 1.0):
            review["source_independence"]["provenance"]["confidence"] = confidence
            self.assertTrue(decide(claim, review, context).eligible)
            self.assertEqual(review["assessment"]["score"], 0.7)
            self.assertEqual(claim["assessment"]["score"], 0.7)

    def test_unassessed_world_claim_is_not_promoted_by_high_provenance_confidence(self):
        claim, review, context = demo_adopted()
        review["assessment"].update(status="unknown", score=None)
        claim["assessment"] = deepcopy(review["assessment"])
        review["provenance"]["confidence"] = 1.0
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("world_claim_unassessed", result.reasons)

    def test_authorization_path_and_resolved_references_are_separate_requirements(self):
        for flag, reason in (("review_authorized", "review_not_authorized"),
                             ("path_eligible", "off_path"),
                             ("references_resolvable", "references_unresolved"),
                             ("conditions_satisfied", "conditions_not_satisfied")):
            with self.subTest(flag=flag):
                claim, review, context = demo_adopted()
                context[flag] = False
                self.assertIn(reason, decide(claim, review, context).reasons)
        _, _, context = demo_adopted()
        context["review_authorized"] = "true"
        with self.assertRaises(ValidationError):
            AdoptionContext.model_validate(context)

    def test_review_due_boundary_is_exclusive_and_cannot_precede_review(self):
        claim, review, context = demo_adopted()
        context["evaluated_at"] = DUE
        self.assertIn("review_outside_valid_time", decide(claim, review, context).reasons)
        context["evaluated_at"] = SEARCHED
        self.assertIn("review_outside_valid_time", decide(claim, review, context).reasons)
        review["review_after"] = REVIEWED
        with self.assertRaises(ValidationError):
            KnowledgeReview.model_validate(review)
        review["review_after"] = "2026-01-01T11:00:00"
        with self.assertRaises(ValidationError):
            KnowledgeReview.model_validate(review)

    def test_proposition_applicability_is_separate_from_review_freshness(self):
        for change, reason in (({"valid_from": "2026-01-01T10:30:00Z"}, "not_yet_applicable"),
                               ({"valid_until": EVALUATED}, "no_longer_applicable")):
            with self.subTest(change=change):
                claim, review, context = demo_adopted()
                claim["proposition"].update(change)
                self.assertIn(reason, decide(claim, review, context).reasons)

    def test_historical_applicability_does_not_rewind_review_freshness(self):
        claim, review, context = demo_adopted()
        claim["proposition"].update(valid_from="2025-12-01T00:00:00Z", valid_until="2025-12-02T00:00:00Z")
        context["applicable_at"] = "2025-12-01T12:00:00Z"
        self.assertTrue(decide(claim, review, context).eligible)
        context["evaluated_at"] = "2026-01-01T11:00:01Z"
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("review_outside_valid_time", result.reasons)
        self.assertNotIn("no_longer_applicable", result.reasons)
        context["applicable_at"] = "2025-12-01T12:00:00"
        with self.assertRaises(ValidationError):
            AdoptionContext.model_validate(context)

    def test_fiction_requires_a_sandbox_and_cannot_leak_to_world_scope(self):
        scope = demo_candidate()["scope"]
        for change in ({"domain": "fiction"}, {"sandbox_id": "demo:sandbox"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                KnowledgeScope.model_validate({**scope, **change})
        claim, review, context = demo_adopted()
        fictional_scope = {**scope, "domain": "fiction", "sandbox_id": "demo:sandbox"}
        claim["scope"] = deepcopy(fictional_scope)
        review["scope"] = deepcopy(fictional_scope)
        for item in review["evidence"] + review["counterevidence"]["searches"]:
            item["scope"] = deepcopy(fictional_scope)
        self.assertIn("scope_mismatch", decide(claim, review, context).reasons)
        context["scope"] = fictional_scope
        self.assertTrue(decide(claim, review, context).eligible)

    def test_withdrawn_claim_requires_explicit_reopening_of_exact_withdrawal(self):
        claim, review, context = demo_adopted()
        withdrawal = {"id": "demo:withdrawal", "version": 2}
        claim["withdrawal_review_ref"] = deepcopy(withdrawal)
        context["withdrawal_ref"] = deepcopy(withdrawal)
        context["withdrawal_created_at"] = SEARCHED
        self.assertIn("withdrawal_not_reopened", decide(claim, review, context).reasons)
        review["reopens_withdrawal_ref"] = {**withdrawal, "version": 1}
        self.assertIn("withdrawal_not_reopened", decide(claim, review, context).reasons)
        review["reopens_withdrawal_ref"] = deepcopy(withdrawal)
        self.assertTrue(decide(claim, review, context).eligible)
        context["withdrawal_ref"] = None
        context["withdrawal_created_at"] = None
        reasons = decide(claim, review, context).reasons
        self.assertIn("withdrawal_snapshot_mismatch", reasons)
        self.assertIn("unknown_withdrawal", reasons)

    def test_adoption_cannot_reopen_a_later_withdrawal(self):
        claim, review, context = demo_adopted()
        withdrawal = {"id": "demo:withdrawal", "version": 1}
        claim["withdrawal_review_ref"] = deepcopy(withdrawal)
        review["reopens_withdrawal_ref"] = deepcopy(withdrawal)
        context.update(withdrawal_ref=deepcopy(withdrawal), withdrawal_created_at=EVALUATED)
        self.assertIn("adoption_predates_withdrawal", decide(claim, review, context).reasons)
        context["withdrawal_created_at"] = None
        with self.assertRaises(ValidationError):
            AdoptionContext.model_validate(context)

    def test_withdrawal_is_historical_and_cannot_be_an_adopt_review(self):
        claim, review, context = demo_adopted()
        review.update(action="withdraw", state_after="withdrawal_candidate")
        claim["knowledge_state"] = "withdrawal_candidate"
        self.assertIn("not_adopted", decide(claim, review, context).reasons)
        review["reopens_withdrawal_ref"] = {"id": "demo:withdrawal", "version": 1}
        with self.assertRaises(ValidationError):
            KnowledgeReview.model_validate(review)

    def test_hold_cannot_clear_a_withdrawal(self):
        claim, review, context = demo_adopted()
        withdrawal = {"id": "demo:withdrawal", "version": 1}
        review.update(action="hold", state_after="provisional")
        claim.update(knowledge_state="provisional", withdrawal_review_ref=deepcopy(withdrawal))
        context.update(withdrawal_ref=deepcopy(withdrawal), withdrawal_created_at=SEARCHED)
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("withdrawal_not_reopened", result.reasons)
        self.assertIn("not_adopted", result.reasons)
        review["reopens_withdrawal_ref"] = deepcopy(withdrawal)
        with self.assertRaises(ValidationError):
            KnowledgeReview.model_validate(review)

    def test_current_review_and_assessment_snapshot_cannot_be_swapped(self):
        claim, review, context = demo_adopted()
        claim["latest_review_ref"]["version"] = 2
        self.assertIn("review_not_current", decide(claim, review, context).reasons)
        claim["latest_review_ref"] = deepcopy(review["ref"])
        claim["assessment"]["score"] = 0.9
        self.assertIn("assessment_snapshot_mismatch", decide(claim, review, context).reasons)

    def test_gate_is_pure_and_revalidates_nested_mutable_assessment(self):
        raw = demo_adopted()
        models = (ClaimSnapshot.model_validate(raw[0]), KnowledgeReview.model_validate(raw[1]),
                  AdoptionContext.model_validate(raw[2]))
        before = [deepcopy(item.model_dump()) for item in models]
        self.assertTrue(adoption_decision(*models).eligible)
        self.assertEqual([item.model_dump() for item in models], before)
        models[1].assessment.evidence_refs.append("demo:missing-evidence")
        with self.assertRaises(ValidationError):
            adoption_decision(*models)


class CounterevidenceContractTests(unittest.TestCase):
    def test_supporting_evidence_cannot_be_recorded_as_counterevidence(self):
        _, review, _ = demo_adopted()
        review["counterevidence"].update(
            status="potential", evidence_refs=[deepcopy(review["evidence"][0]["ref"])])
        with self.assertRaises(ValidationError):
            KnowledgeReview.model_validate(review)

    def test_blocked_search_is_not_no_counterevidence_but_can_preserve_a_finding(self):
        claim, review, context = demo_adopted()
        search = review["counterevidence"]["searches"][0]
        search.update(status="blocked", completed_at=None, stop_reason="Demo source unavailable")
        CounterevidenceSearch.model_validate(search)
        with self.assertRaises(ValidationError):
            CounterevidenceReview.model_validate(review["counterevidence"])
        item = counterexample(review)
        review["counterevidence"].update(status="material_unresolved", evidence_refs=[item["ref"]])
        claim["counterevidence_status"] = "material_unresolved"
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("counterevidence_unresolved", result.reasons)
        self.assertIn("countersearch_incomplete", result.reasons)

    def test_completed_search_needs_execution_coverage_and_completion_time(self):
        for field, value in (("executed_queries", []), ("coverage", " "), ("completed_at", None)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                _, review, _ = demo_adopted()
                search = review["counterevidence"]["searches"][0]
                search[field] = value
                CounterevidenceSearch.model_validate(search)

    def test_a_completed_search_does_not_hide_another_pending_search(self):
        claim, review, context = demo_adopted()
        pending = deepcopy(review["counterevidence"]["searches"][0])
        pending.update(ref={"id": "demo:pending-search", "version": 1}, status="deferred",
                       completed_at=None, stop_reason="Demo information gain below threshold")
        review["counterevidence"]["searches"].append(pending)
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("countersearch_pending", result.reasons)

    def test_no_counterevidence_cannot_coexist_with_reviewed_refutation_or_qualification(self):
        for relation in ("refutes", "qualifies"):
            with self.subTest(relation=relation), self.assertRaises(ValidationError):
                _, review, _ = demo_adopted()
                counterexample(review, relation=relation)
                KnowledgeReview.model_validate(review)

    def test_pending_negative_evidence_blocks_adoption_even_after_empty_search(self):
        for relation in ("refutes", "qualifies"):
            for content_check in ("unreviewed", "uncertain"):
                with self.subTest(relation=relation, content_check=content_check):
                    claim, review, context = demo_adopted()
                    counterexample(review, relation=relation, content_check=content_check)
                    result = decide(claim, review, context)
                    self.assertFalse(result.eligible)
                    self.assertIn("counterevidence_pending", result.reasons)

    def test_addressed_review_must_cover_every_matching_counterevidence_item(self):
        claim, review, context = demo_adopted()
        first = counterexample(review)
        second = deepcopy(first)
        second.update(ref={"id": "demo:qualification", "version": 1}, relation="qualifies")
        review["evidence"].append(second)
        review["counterevidence"].update(
            status="addressed", evidence_refs=[first["ref"]],
            resolution="The first demo item refers to a different instrument.")
        claim["counterevidence_status"] = "addressed"
        result = decide(claim, review, context)
        self.assertFalse(result.eligible)
        self.assertIn("counterevidence_not_addressed", result.reasons)
        review["counterevidence"]["evidence_refs"].append(second["ref"])
        review["counterevidence"]["resolution"] = "Both demo items concern an excluded instrument."
        self.assertTrue(decide(claim, review, context).eligible)

    def test_review_cannot_claim_future_search_evidence_or_checks(self):
        claim, review, context = demo_adopted()
        claim["provenance"]["created_at"] = EVALUATED
        self.assertIn("review_predates_claim", decide(claim, review, context).reasons)
        claim, review, context = demo_adopted()
        review["counterevidence"]["searches"][0]["completed_at"] = EVALUATED
        self.assertIn("review_predates_search", decide(claim, review, context).reasons)
        claim, review, context = demo_adopted()
        review["evidence"][0]["provenance"]["created_at"] = EVALUATED
        self.assertIn("review_predates_evidence", decide(claim, review, context).reasons)
        for check in ("counterevidence", "source_independence"):
            with self.subTest(check=check):
                claim, review, context = demo_adopted()
                review[check]["provenance"]["created_at"] = EVALUATED
                self.assertIn("review_predates_checks", decide(claim, review, context).reasons)


if __name__ == "__main__":
    unittest.main()

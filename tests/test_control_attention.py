"""Pure P6/P7 contracts: fictional inputs; no DB, network, model, or service."""
from copy import deepcopy
import unittest

from pydantic import ValidationError

from v02.attention import (AttentionContext, AttentionProposal, compare_shadow,
                           rule_plan, select_plan, validate_proposal)
from v02.control_policy import ControlAssessmentInput, evaluate_control


AT = "2026-01-01T10:00:00Z"
DUE = "2026-01-01T11:00:00Z"
SCOPE = {"conversation_id": "demo:conversation", "context_key": "demo:context", "sandbox_id": None}


def control(**changes):
    return {"idempotency_key": "demo:control", "reason": "Fictional structured controller evidence",
            "expected_version": 2, "scope": deepcopy(SCOPE), "path_revision": "demo:path-v1", "expiry": DUE,
            "evidence_refs": ["demo:observed-event"], **changes}


def attention(**changes):
    return {"scope": deepcopy(SCOPE), "path_revision": "demo:path-v1", "query": "blue beacon",
            "evaluated_at": AT, "requested_hops": 1, "node_budget": 20,
            "candidates": [{"id": identity, "version": 1, "scope": deepcopy(SCOPE)}
                           for identity in ("demo:a", "demo:b", "demo:c")],
            "rule_candidate_refs": [{"id": "demo:a", "version": 1}], **changes}


def proposal(**changes):
    return {"scope": deepcopy(SCOPE), "path_revision": "demo:path-v1", "direction": ["beacon", "blue"],
            "candidate_refs": [{"id": "demo:b", "version": 1}, {"id": "demo:a", "version": 1}],
            "graph_hops": 2, "node_budget": 10, "source_priority": False, "risk": "uncertain",
            "reason": "Fictional planner suggests inspecting a second beacon record", "model_id": "demo:model",
            **changes}


class ControlPolicyTests(unittest.TestCase):
    def test_zero_signal_is_not_a_survival_or_completion_objective(self):
        result = evaluate_control(control(), evaluated_at=AT)
        self.assertTrue(all(value == 0 for value in result.values.model_dump().values()))
        self.assertEqual(result.preservation_constraints, ())
        self.assertIsNone(result.suggested_unresolved_status)
        self.assertFalse(result.survival_objective)
        self.assertFalse(result.automatic_knowledge_change)
        self.assertFalse(result.automatic_state_change)

    def test_prediction_unknown_and_preservation_inputs_remain_distinct(self):
        result = evaluate_control(control(prediction_error=.4, unknown_fraction=.7,
            memory_integrity_pressure=.3, storage_pressure=.5, environment_pressure=.9,
            expected_information_gain=.8), evaluated_at=AT)
        self.assertEqual(result.values.uncertainty_load, .7)
        self.assertEqual(result.values.information_gap, .7)
        self.assertEqual(result.values.integrity_pressure, .9)
        self.assertEqual(result.values.attention_urgency, .9)
        self.assertAlmostEqual(result.values.exploration_readiness, .56)
        self.assertEqual(set(result.preservation_constraints),
            {"verify_memory_integrity", "preserve_storage_margin", "prepare_state_checkpoint"})
        self.assertEqual(set(result.exploration_advice), {"inspect_prediction_error", "seek_missing_information"})

    def test_loss_control_and_reversibility_jointly_determine_pressure(self):
        high = evaluate_control(control(loss_magnitude=.8, controllability=0., reversibility=0.), evaluated_at=AT)
        controllable = evaluate_control(control(loss_magnitude=.8, controllability=1., reversibility=0.), evaluated_at=AT)
        reversible = evaluate_control(control(loss_magnitude=.8, controllability=0., reversibility=1.), evaluated_at=AT)
        both = evaluate_control(control(loss_magnitude=.8, controllability=1., reversibility=1.), evaluated_at=AT)
        self.assertGreater(high.risk_pressure, controllable.risk_pressure)
        self.assertGreater(high.risk_pressure, reversible.risk_pressure)
        self.assertLess(both.risk_pressure, controllable.risk_pressure)
        self.assertGreater(both.risk_pressure, 0)

    def test_cognitive_influence_changes_only_caution(self):
        low = evaluate_control(control(prediction_error=.1), evaluated_at=AT).model_dump()
        high = evaluate_control(control(prediction_error=.1, cognitive_influence=1.), evaluated_at=AT).model_dump()
        self.assertGreater(high["values"].pop("caution_gain"), low["values"].pop("caution_gain"))
        self.assertEqual(low, high)
        self.assertNotIn("sandbox", high)

    def test_low_information_gain_only_suggests_deferring_active_work(self):
        req = control(prediction_error=1., expected_information_gain=.01, unresolved_status="active")
        original = deepcopy(req)
        result = evaluate_control(req, evaluated_at=AT)
        self.assertEqual(result.suggested_unresolved_status, "deferred")
        self.assertIn("defer_low_information_gain", result.exploration_advice)
        self.assertEqual(req, original)
        self.assertEqual(req["unresolved_status"], "active")
        boundary = evaluate_control(control(expected_information_gain=.05, unresolved_status="active"), evaluated_at=AT)
        self.assertIsNone(boundary.suggested_unresolved_status)

    def test_blocked_deferred_and_finished_work_does_not_auto_resume(self):
        for status in ("blocked", "deferred", "resolved", "abandoned"):
            with self.subTest(status=status):
                result = evaluate_control(control(prediction_error=1., expected_information_gain=1.,
                                                  unresolved_status=status), evaluated_at=AT)
                self.assertEqual(result.values.exploration_readiness, 0.)
                self.assertIsNone(result.suggested_unresolved_status)
                self.assertIn("respect_existing_pause", result.exploration_advice)

    def test_unknown_eig_is_explicit_and_never_truth_confidence(self):
        result = evaluate_control(control(unknown_fraction=.8), evaluated_at=AT, source="demo:authorized-controller")
        self.assertIn("estimate_information_gain", result.exploration_advice)
        self.assertEqual(result.scope.model_dump(), SCOPE)
        self.assertEqual(result.path_revision, "demo:path-v1")
        self.assertEqual(result.expiry, DUE)
        self.assertEqual(result.provenance.confidence_target, "functional_control")
        self.assertIsNone(result.provenance.confidence)
        self.assertEqual(result.provenance.created_at, AT)
        self.assertEqual(result.provenance.expiry, DUE)
        self.assertEqual(result.provenance.cause_refs, ["demo:observed-event"])
        for field in ("retrieval_weight", "generalization_weight", "salience", "assessment", "knowledge_state"):
            self.assertNotIn(field, result.model_dump())

    def test_affect_capability_survival_and_nonfinite_inputs_are_rejected(self):
        for changes in ({"fear": .9}, {"self_worth": .1}, {"capability": .1}, {"survival_reward": 1.},
                        {"prediction_error": float("nan")}, {"storage_pressure": float("inf")},
                        {"unknown_fraction": True}, {"expected_version": True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ControlAssessmentInput.model_validate(control(**changes))

    def test_expired_unscoped_and_missing_evidence_are_rejected(self):
        for changes in ({"expiry": AT}, {"expiry": "2026-01-01T09:00:00Z"},
                        {"expiry": "2026-01-01T12:00:00"}, {"scope": {}}, {"evidence_refs": []},
                        {"evidence_refs": ["demo:event", "demo:event"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evaluate_control(control(**changes), evaluated_at=AT)


class AttentionPolicyTests(unittest.TestCase):
    def test_rule_preserves_existing_directions_depth_and_provenance(self):
        context = attention(exploration_readiness=1., modifier_ids=["demo:modifier"], state_ids=["demo:state"])
        result = rule_plan(context)
        self.assertEqual(result.direction, ("beacon", "blue"))
        self.assertEqual(result.graph_hops, 3)
        self.assertEqual(result.version, "attention.rules.v0.2")
        self.assertEqual(result.risk, "not_assessed")
        self.assertEqual(result.context_scope, "demo:context")
        self.assertEqual(result.provenance.cause_refs, ["demo:modifier", "demo:state"])
        self.assertIsNone(result.provenance.confidence)
        self.assertEqual(result.provenance.created_at, AT)
        self.assertTrue(result.provenance.decay["reason"])

    def test_rule_requires_no_model_or_candidate_pool(self):
        context = attention(candidates=[], rule_candidate_refs=[], query="")
        result = select_plan(context)
        self.assertEqual(result.fallback_reason, "no_proposal")
        self.assertFalse(result.proposal_accepted)
        self.assertEqual(result.plan.direction, ())
        self.assertEqual(result.plan.candidate_refs, ())

    def test_valid_proposal_is_bounded_recommendation_not_truth_or_authority(self):
        result = validate_proposal(attention(), proposal())
        self.assertEqual(result.planner_kind, "validated_proposal")
        self.assertEqual([ref.id for ref in result.candidate_refs], ["demo:b", "demo:a"])
        self.assertEqual(result.graph_hops, 2)
        self.assertEqual(result.risk, "uncertain")
        self.assertIsNone(result.provenance.confidence)
        self.assertEqual(result.provenance.confidence_target, "attention_plan")
        self.assertEqual(result.provenance.cause_refs, ["demo:b", "demo:a"])

    def test_unknown_uid_stale_version_and_cross_scope_fall_back(self):
        for changes in ({"candidate_refs": [{"id": "demo:unknown", "version": 1}]},
                        {"candidate_refs": [{"id": "demo:a", "version": 2}]},
                        {"scope": {**SCOPE, "conversation_id": "demo:other"}},
                        {"scope": {**SCOPE, "context_key": "demo:other"}},
                        {"scope": {**SCOPE, "sandbox_id": "demo:fiction"}},
                        {"path_revision": "demo:old-path"}):
            with self.subTest(changes=changes):
                result = select_plan(attention(), proposal(**changes))
                self.assertEqual(result.fallback_reason, "invalid_proposal")
                self.assertEqual(result.plan, rule_plan(attention()))

    def test_candidate_scope_cannot_be_laundered_by_proposal_scope(self):
        context = attention()
        context["candidates"][1]["scope"]["sandbox_id"] = "demo:story"
        result = select_plan(context, proposal())
        self.assertFalse(result.proposal_accepted)
        self.assertEqual(result.fallback_reason, "invalid_proposal")

    def test_hard_limits_and_source_review_cannot_be_overridden(self):
        for context, changes in ((attention(max_hops=1), {}), (attention(overgeneralization=True), {}),
                                 (attention(source_priority=True), {}),
                                 (attention(), {"node_budget": 21}),
                                 (attention(), {"node_budget": 1}),
                                 (attention(), {"graph_hops": 4}),
                                 (attention(), {"graph_hops": True})):
            with self.subTest(context=context, changes=changes):
                self.assertEqual(select_plan(context, proposal(**changes)).fallback_reason, "invalid_proposal")
        result = validate_proposal(attention(overgeneralization=True, source_priority=True),
                                   proposal(graph_hops=0, source_priority=True))
        self.assertEqual(result.graph_hops, 0)
        self.assertTrue(result.source_priority)

    def test_malformed_model_output_and_attempted_state_changes_fall_back(self):
        for value in ("not JSON", {}, proposal(risk="safe"), proposal(direction=[""]),
                      proposal(direction=["blue", "blue"]), proposal(retrieval_weight=1.),
                      proposal(assessment={"score": 1.}), proposal(modifier_ids=[]),
                      proposal(candidate_refs=[{"id": "demo:a", "version": 1}] * 2)):
            with self.subTest(value=value):
                self.assertEqual(select_plan(attention(), value).fallback_reason, "invalid_proposal")

    def test_invalid_canonical_context_fails_instead_of_hiding_caller_error(self):
        for context in (attention(evaluated_at="2026-01-01T10:00:00"),
                        attention(rule_candidate_refs=[{"id": "demo:unknown", "version": 1}]),
                        attention(modifier_ids=["demo:same", "demo:same"]),
                        attention(candidates=[{"id": "demo:a", "version": 1, "scope": SCOPE}] * 2)):
            with self.subTest(context=context), self.assertRaises(ValueError):
                select_plan(context, proposal())

    def test_shadow_reports_selection_differences_without_mutating_inputs(self):
        context, value = attention(), proposal(direction=["beacon", "harbor"])
        before_context, before_value = deepcopy(context), deepcopy(value)
        result = compare_shadow(context, value)
        self.assertEqual([ref.id for ref in result.added_candidate_refs], ["demo:b"])
        self.assertEqual(result.removed_candidate_refs, ())
        self.assertTrue(result.candidate_order_changed)
        self.assertEqual(result.added_directions, ("harbor",))
        self.assertEqual(result.removed_directions, ("blue",))
        self.assertEqual(result.graph_hops_delta, 1)
        self.assertEqual(result.node_budget_delta, -10)
        self.assertFalse(result.affects_normal_selection)
        self.assertFalse(result.writes_persistent_state)
        self.assertEqual(len(result.proposal_input_digest), 64)
        self.assertEqual(context, before_context)
        self.assertEqual(value, before_value)

    def test_bad_shadow_proposal_does_not_leak_raw_output_or_change_baseline(self):
        bad = proposal(graph_hops=float("nan"), reason="demo secret-looking but fictional input")
        result = compare_shadow(attention(), bad)
        self.assertEqual(result.baseline, result.proposal)
        self.assertIsNone(result.proposal_input_digest)
        self.assertNotIn("secret-looking", result.model_dump_json())


if __name__ == "__main__":
    unittest.main()

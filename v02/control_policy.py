"""Pure functional-control policy for evidence-backed structured assessments.

The authenticated caller resolves evidence, scope, and expected_version before
persisting these results through the existing state command. Values are bounded
heuristics, not emotion, truth confidence, or an objective to keep running.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Command, Contract, Provenance, Scope, Score, timestamp


CONTROL_POLICY = "functional.control.heuristic.v1"
LOW_INFORMATION_GAIN = 0.05
Text = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
UnresolvedStatus = Literal["active", "blocked", "deferred", "resolved", "abandoned"]


class ControlAssessmentInput(Command):
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    scope: Scope
    path_revision: Text
    expiry: Text
    evidence_refs: tuple[Text, ...] = Field(min_length=1)
    prediction_error: Score = 0.0
    unknown_fraction: Score = 0.0
    memory_integrity_pressure: Score = 0.0
    storage_pressure: Score = 0.0
    environment_pressure: Score = 0.0
    loss_magnitude: Score = 0.0
    controllability: Score = 1.0
    reversibility: Score = 1.0
    cognitive_influence: Score = 0.0
    expected_information_gain: Score | None = None
    unresolved_status: UnresolvedStatus | None = None

    @model_validator(mode="after")
    def resolved_shape(self):
        timestamp(self.expiry)
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("Repeated control evidence")
        return self


class FunctionalValues(Contract):
    uncertainty_load: Score
    attention_urgency: Score
    exploration_readiness: Score
    caution_gain: Score
    integrity_pressure: Score
    information_gap: Score


class ControlEvaluation(Contract):
    scope: Scope
    path_revision: Text
    expiry: Text
    values: FunctionalValues
    risk_pressure: Score
    preservation_constraints: tuple[Literal[
        "verify_memory_integrity", "preserve_storage_margin", "prepare_state_checkpoint"], ...]
    exploration_advice: tuple[Literal[
        "inspect_prediction_error", "seek_missing_information", "defer_low_information_gain",
        "respect_existing_pause", "estimate_information_gain"], ...]
    suggested_unresolved_status: Literal["deferred"] | None
    provenance: Provenance
    assessment_semantics: Literal["uncalibrated_control_heuristic"] = "uncalibrated_control_heuristic"
    automatic_state_change: Literal[False] = False
    automatic_knowledge_change: Literal[False] = False
    survival_objective: Literal[False] = False


def evaluate_control(value: ControlAssessmentInput | dict, *, evaluated_at: str,
                     source: str = "structured_controller") -> ControlEvaluation:
    """Evaluate once at an explicit time; this function never authorizes or writes.

    Uncertainty combines declared prediction error and missing information.
    Control/reversibility attenuate loss pressure; influence only affects caution.
    The EIG threshold and coefficients are versioned initial evaluation settings.
    Unknown EIG leaves exploration available with an explicit estimate request.
    """
    req = ControlAssessmentInput.model_validate(
        value.model_dump() if isinstance(value, ControlAssessmentInput) else value)
    at = timestamp(evaluated_at)
    if timestamp(req.expiry) <= at:
        raise ValueError("Control assessment has expired")
    uncertainty = max(req.prediction_error, req.unknown_fraction)
    integrity = max(req.memory_integrity_pressure, req.storage_pressure, req.environment_pressure)
    risk = req.loss_magnitude * (1.0 - .5 * req.controllability) * (1.0 - .5 * req.reversibility)
    expected_gain = req.expected_information_gain
    paused = req.unresolved_status in {"blocked", "deferred", "resolved", "abandoned"}
    readiness = uncertainty * (expected_gain if expected_gain is not None else 1.0)
    if paused:
        readiness = 0.0
    constraints, advice = [], []
    for pressure, constraint in (
        (req.memory_integrity_pressure, "verify_memory_integrity"),
        (req.storage_pressure, "preserve_storage_margin"),
        (req.environment_pressure, "prepare_state_checkpoint"),
    ):
        if pressure > 0:
            constraints.append(constraint)
    if req.prediction_error > 0:
        advice.append("inspect_prediction_error")
    if req.unknown_fraction > 0:
        advice.append("seek_missing_information")
    suggested = None
    if paused:
        advice.append("respect_existing_pause")
    elif expected_gain is not None and expected_gain < LOW_INFORMATION_GAIN:
        advice.append("defer_low_information_gain")
        if req.unresolved_status == "active":
            suggested = "deferred"
    elif expected_gain is None and uncertainty > 0:
        advice.append("estimate_information_gain")
    return ControlEvaluation(scope=req.scope, path_revision=req.path_revision, expiry=req.expiry,
        values=FunctionalValues(uncertainty_load=uncertainty,
            attention_urgency=max(uncertainty, integrity, risk), exploration_readiness=readiness,
            caution_gain=max(uncertainty, risk, req.cognitive_influence),
            integrity_pressure=integrity, information_gap=req.unknown_fraction),
        risk_pressure=risk, preservation_constraints=tuple(constraints), exploration_advice=tuple(advice),
        suggested_unresolved_status=suggested,
        provenance=Provenance(source=source, reason=req.reason, confidence=None,
            confidence_target="functional_control", created_at=evaluated_at, expiry=req.expiry,
            decay={"kind": "none", "reason": "scope_expiry_controls_effect"},
            policy_version=CONTROL_POLICY, cause_refs=list(req.evidence_refs)))

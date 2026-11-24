"""Typed rule attention and non-mutating model-proposal/shadow boundaries.

No model, database, credentials, or learning is called here. The host resolves
an authorized candidate pool; this module checks its scope and exact versions.
Model recommendations are data and cannot revise weights, truth, or permissions.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import Contract, Provenance, Scope, Score, digest, timestamp
from .knowledge_contracts import RecordRef
from .policy import terms


ATTENTION_RULE_POLICY = "attention.rules.v0.2"
ATTENTION_PROPOSAL_POLICY = "attention.proposal.v1"
ATTENTION_SHADOW_POLICY = "attention.shadow.v1"
Text = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
Direction = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"\S")]


class AttentionCandidate(Contract):
    id: Text
    version: int = Field(ge=1, strict=True)
    scope: Scope

    def ref(self) -> RecordRef:
        return RecordRef(id=self.id, version=self.version)


class AttentionContext(Contract):
    scope: Scope
    path_revision: Text
    query: str
    evaluated_at: Text
    candidates: tuple[AttentionCandidate, ...] = ()
    rule_candidate_refs: tuple[RecordRef, ...] = ()
    requested_hops: int = Field(default=0, ge=0, le=3, strict=True)
    max_hops: int = Field(default=3, ge=0, le=3, strict=True)
    node_budget: int = Field(default=100, ge=1, le=10000, strict=True)
    modifier_ids: tuple[Text, ...] = ()
    state_ids: tuple[Text, ...] = ()
    source_priority: bool = False
    overgeneralization: bool = False
    exploration_readiness: Score = 0.0

    @model_validator(mode="after")
    def canonical_context(self):
        timestamp(self.evaluated_at)
        if len({item.id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate IDs must name one canonical version each")
        for values in (self.modifier_ids, self.state_ids):
            if len(set(values)) != len(values):
                raise ValueError("Repeated attention cause")
        _check_refs(self.rule_candidate_refs, self)
        return self


class AttentionProposal(Contract):
    scope: Scope
    path_revision: Text
    direction: tuple[Direction, ...] = Field(min_length=1, max_length=64)
    candidate_refs: tuple[RecordRef, ...] = Field(default=(), max_length=10000)
    graph_hops: int = Field(ge=0, le=3, strict=True)
    node_budget: int = Field(ge=1, le=10000, strict=True)
    source_priority: bool = False
    risk: Literal["not_assessed", "uncertain"] = "not_assessed"
    reason: Text
    model_id: Text

    @model_validator(mode="after")
    def unique_direction(self):
        if len(set(self.direction)) != len(self.direction):
            raise ValueError("Repeated search direction")
        return self


class AttentionPlan(Contract):
    version: Literal["attention.rules.v0.2", "attention.proposal.v1"]
    direction: tuple[Text, ...]
    graph_hops: int = Field(ge=0, le=3, strict=True)
    source_priority: bool
    context_scope: Text
    risk: Literal["not_assessed", "uncertain"]
    state_ids: tuple[Text, ...]
    modifier_ids: tuple[Text, ...]
    provenance: Provenance
    scope: Scope
    path_revision: Text
    node_budget: int = Field(ge=1, le=10000, strict=True)
    candidate_refs: tuple[RecordRef, ...]
    planner_kind: Literal["rule", "validated_proposal"]
    model_id: Text | None = None


class PlanSelection(Contract):
    plan: AttentionPlan
    proposal_accepted: bool
    fallback_reason: Literal["no_proposal", "invalid_proposal"] | None


class ShadowComparison(Contract):
    policy_version: Literal["attention.shadow.v1"] = ATTENTION_SHADOW_POLICY
    baseline: AttentionPlan
    proposal: AttentionPlan
    proposal_accepted: bool
    fallback_reason: Literal["no_proposal", "invalid_proposal"] | None
    added_candidate_refs: tuple[RecordRef, ...]
    removed_candidate_refs: tuple[RecordRef, ...]
    candidate_order_changed: bool
    added_directions: tuple[Text, ...]
    removed_directions: tuple[Text, ...]
    graph_hops_delta: int
    node_budget_delta: int
    proposal_input_digest: str | None
    provenance: Provenance
    affects_normal_selection: Literal[False] = False
    writes_persistent_state: Literal[False] = False


def _context(value: AttentionContext | dict) -> AttentionContext:
    return AttentionContext.model_validate(value.model_dump() if isinstance(value, AttentionContext) else value)


def _check_refs(refs, context):
    if len({ref.id for ref in refs}) != len(refs):
        raise ValueError("Duplicate candidate recommendation")
    pool = {item.id: item for item in context.candidates}
    for ref in refs:
        item = pool.get(ref.id)
        if item is None or item.ref() != ref:
            raise ValueError("Candidate is absent or its canonical version changed")
        if item.scope != context.scope:
            raise ValueError("Candidate is outside the selected scope")


def _provenance(context, source, reason, policy, *, extra_causes=()):
    return Provenance(source=source, reason=reason, confidence=None,
        confidence_target="attention_plan", created_at=context.evaluated_at,
        decay={"kind": "none", "reason": "historical_plan_bound_to_one_run_and_path_revision"},
        policy_version=policy,
        cause_refs=list(dict.fromkeys((*context.modifier_ids, *context.state_ids, *extra_causes))))


def rule_plan(value: AttentionContext | dict) -> AttentionPlan:
    context = _context(value)
    hops = 0 if context.overgeneralization else min(context.max_hops,
        context.requested_hops + int(context.exploration_readiness * 2))
    return AttentionPlan(version=ATTENTION_RULE_POLICY, direction=tuple(sorted(terms(context.query))),
        graph_hops=hops, source_priority=context.source_priority, context_scope=context.scope.context_key,
        risk="not_assessed", state_ids=context.state_ids, modifier_ids=context.modifier_ids,
        provenance=_provenance(context, "rule_frontend", "query_and_context_direction", ATTENTION_RULE_POLICY),
        scope=context.scope, path_revision=context.path_revision, node_budget=context.node_budget,
        candidate_refs=context.rule_candidate_refs, planner_kind="rule")


def validate_proposal(value: AttentionContext | dict, proposal: AttentionProposal | dict) -> AttentionPlan:
    context = _context(value)
    req = AttentionProposal.model_validate(proposal.model_dump() if isinstance(proposal, AttentionProposal) else proposal)
    if req.scope != context.scope or req.path_revision != context.path_revision:
        raise ValueError("Attention proposal crosses scope or selected path revision")
    _check_refs(req.candidate_refs, context)
    if req.graph_hops > context.max_hops or (context.overgeneralization and req.graph_hops != 0):
        raise ValueError("Attention proposal exceeds the current exploration limit")
    if req.node_budget > context.node_budget or len(req.candidate_refs) > req.node_budget:
        raise ValueError("Attention proposal exceeds the allowed node budget")
    if context.source_priority and not req.source_priority:
        raise ValueError("A model proposal cannot remove a required source review")
    return AttentionPlan(version=ATTENTION_PROPOSAL_POLICY, direction=req.direction, graph_hops=req.graph_hops,
        source_priority=req.source_priority, context_scope=context.scope.context_key, risk=req.risk,
        state_ids=context.state_ids, modifier_ids=context.modifier_ids,
        provenance=_provenance(context, "model_proposal:" + req.model_id, req.reason,
            ATTENTION_PROPOSAL_POLICY, extra_causes=[ref.id for ref in req.candidate_refs]),
        scope=context.scope, path_revision=context.path_revision, node_budget=req.node_budget,
        candidate_refs=req.candidate_refs, planner_kind="validated_proposal", model_id=req.model_id)


def select_plan(value: AttentionContext | dict, proposal: AttentionProposal | dict | None = None) -> PlanSelection:
    context = _context(value)
    baseline = rule_plan(context)
    if proposal is None:
        return PlanSelection(plan=baseline, proposal_accepted=False, fallback_reason="no_proposal")
    try:
        proposed = validate_proposal(context, proposal)
    except (ValidationError, ValueError, TypeError):
        return PlanSelection(plan=baseline, proposal_accepted=False, fallback_reason="invalid_proposal")
    return PlanSelection(plan=proposed, proposal_accepted=True, fallback_reason=None)


def compare_shadow(value: AttentionContext | dict, proposal: AttentionProposal | dict | None) -> ShadowComparison:
    context = _context(value)
    baseline = rule_plan(context)
    selection = select_plan(context, proposal)
    proposed = selection.plan
    before, after = set(baseline.candidate_refs), set(proposed.candidate_refs)
    original = proposal.model_dump() if isinstance(proposal, AttentionProposal) else proposal
    try:
        proposal_digest = digest(original) if original is not None else None
    except (TypeError, ValueError):
        proposal_digest = None  # Non-JSON/NaN proposals are rejected, not echoed into records.
    return ShadowComparison(baseline=baseline, proposal=proposed, proposal_accepted=selection.proposal_accepted,
        fallback_reason=selection.fallback_reason,
        added_candidate_refs=tuple(ref for ref in proposed.candidate_refs if ref not in before),
        removed_candidate_refs=tuple(ref for ref in baseline.candidate_refs if ref not in after),
        candidate_order_changed=baseline.candidate_refs != proposed.candidate_refs,
        added_directions=tuple(item for item in proposed.direction if item not in baseline.direction),
        removed_directions=tuple(item for item in baseline.direction if item not in proposed.direction),
        graph_hops_delta=proposed.graph_hops - baseline.graph_hops,
        node_budget_delta=proposed.node_budget - baseline.node_budget,
        proposal_input_digest=proposal_digest,
        provenance=_provenance(context, "attention_shadow", "compare_without_applying_recommendations", ATTENTION_SHADOW_POLICY))

"""K4 query-use contracts. Query context cannot assert truth or satisfied conditions."""
from __future__ import annotations

from typing import Literal

from .contracts import Assessment, KnowledgeQueryContext, Provenance
from .knowledge_contracts import (
    ClaimTarget, CounterevidenceStatus, EvidenceSnapshot, IndependenceStatus, KnowledgeContract,
    KnowledgeScope, KnowledgeState, Proposition, RecordRef, Revision, Text,
)


RETRIEVAL_POLICY = "knowledge.retrieval.k4.v2"


class ConditionCheck(KnowledgeContract):
    condition: Text
    status: Literal["unknown", "satisfied", "not_satisfied"]
    source: Text
    reason: Text


class ClaimSourceMaterial(KnowledgeContract):
    source_ref: RecordRef
    scope: KnowledgeScope
    role: Literal["user", "assistant", "tool", "system"]
    actor_id: str | None
    source_digest: Text
    model_provenance: dict
    parent_source_refs: tuple[RecordRef, ...]
    event_refs: tuple[RecordRef, ...]
    origin_ids: tuple[str, ...]
    origin_excerpt: str | None = None
    evidence_excerpts: tuple[dict, ...] = ()


class ReviewExplanation(KnowledgeContract):
    """Resolved history explaining a state, never a query-use certificate."""
    review_ref: RecordRef
    reference_roles: tuple[Literal["latest_review", "withdrawal_ledger"], ...]
    interpretation: Literal["historical_review_not_current_adoption"] = "historical_review_not_current_adoption"
    scope: KnowledgeScope
    target: ClaimTarget
    reviewed_proposition: Proposition
    expected_claim_record_version: Revision
    committed_claim_record_version: Revision
    matches_current_target: bool
    recorded_path_revision: Text
    action: Literal["hold", "adopt", "dispute", "propose_withdrawal", "withdraw"]
    state_after: KnowledgeState
    review_after: str
    reopens_withdrawal_ref: RecordRef | None
    reviewed_evidence: tuple[EvidenceSnapshot, ...]
    search_refs: tuple[RecordRef, ...]
    provenance: Provenance


class ClaimUseSnapshot(KnowledgeContract):
    claim_ref: RecordRef
    target: ClaimTarget
    scope: KnowledgeScope
    proposition: Proposition
    knowledge_state: KnowledgeState
    assessment: Assessment
    counterevidence_status: CounterevidenceStatus
    source_independence: IndependenceStatus
    review_ref: RecordRef | None
    review_after: str | None
    evidence_refs: tuple[RecordRef, ...] = ()
    search_refs: tuple[RecordRef, ...] = ()
    observation_refs: tuple[RecordRef, ...] = ()
    source_material: tuple[ClaimSourceMaterial, ...] = ()
    condition_checks: tuple[ConditionCheck, ...] = ()
    state_explanation: Text
    review_explanations: tuple[ReviewExplanation, ...] = ()
    usage_scope: Literal["adopted_in_declared_scope", "unadopted_do_not_assert_as_truth"]
    generalization_policy: Literal["no_additional_generalization_authorized"] = "no_additional_generalization_authorized"
    reasons: tuple[str, ...] = ()
    applicable_at: str | None
    evaluated_at: str
    provenance: Provenance

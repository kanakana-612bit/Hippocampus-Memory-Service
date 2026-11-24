"""K0 knowledge contracts and a side-effect-free reference adoption gate.

K1 resolves storage and authorization for explicit review; extraction in K2 does
not grant adoption. A valid review describes a reviewer's assertions, not a proof
of truth. Query applicability and model injection remain separate responsibilities.
"""
from __future__ import annotations

from itertools import combinations
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .contracts import Assessment, Contract, Provenance, digest, timestamp


POLICY_VERSION = "knowledge.k0.v1"
Text = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
Revision = Annotated[int, Field(strict=True, ge=1)]
KnowledgeState = Literal["provisional", "established", "disputed", "withdrawal_candidate"]
CounterevidenceStatus = Literal[
    "unassessed", "none_found_in_scope", "potential", "material_unresolved", "addressed"
]
IndependenceStatus = Literal["unknown", "dependent", "partially_independent", "independent"]
SearchStatus = Literal["not_started", "planned", "running", "completed", "blocked", "deferred"]
EvidenceProfile = Literal[
    "independent_observations", "attributed_report", "direct_observation", "formal_derivation"
]


class KnowledgeContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class RecordRef(KnowledgeContract):
    id: Text
    version: Revision


class ClaimTarget(KnowledgeContract):
    claim_id: Text
    claim_revision: Revision
    evidence_set_revision: Revision


class KnowledgeScope(KnowledgeContract):
    principal_id: Text
    conversation_id: Text
    context_key: Text = "conversation"
    domain: Literal["world", "fiction"] = "world"
    sandbox_id: Text | None = None

    @model_validator(mode="after")
    def fiction_boundary(self):
        if (self.domain == "fiction") != bool(self.sandbox_id):
            raise ValueError("Fiction and sandbox scope must be specified together")
        return self


class Proposition(KnowledgeContract):
    text: Text
    kind: Literal["unspecified", "empirical", "attributed_report", "bounded_observation", "formal"]
    subject: Text | None = None
    predicate: Text | None = None
    object: Text | None = None
    polarity: Literal["affirmed", "negated", "unknown"] = "unknown"
    quantifier: Literal["single", "bounded", "universal", "unknown"] = "unknown"
    attribution_actor_id: Text | None = None
    conditions: tuple[Text, ...] = ()
    valid_from: str | None = None
    valid_until: str | None = None

    @model_validator(mode="after")
    def validity(self):
        for value in (self.valid_from, self.valid_until):
            if value is not None:
                timestamp(value)
        if self.valid_from and self.valid_until and timestamp(self.valid_from) >= timestamp(self.valid_until):
            raise ValueError("Validity is a nonempty half-open interval")
        if self.kind == "attributed_report" and not self.attribution_actor_id:
            raise ValueError("An attributed report requires its speaker")
        if len(set(self.conditions)) != len(self.conditions):
            raise ValueError("Duplicate proposition conditions")
        return self

    def exact_fingerprint(self, scope: KnowledgeScope) -> str:
        # Intentionally conservative: no paraphrase, case, time, or entity merging.
        return digest({"proposition": self.model_dump(), "scope": scope.model_dump()})


class HistoricalRecord(KnowledgeContract):
    provenance: Provenance

    @field_validator("provenance")
    @classmethod
    def history_provenance(cls, value):
        timestamp(value.created_at)
        if value.expiry is not None:
            raise ValueError("Historical evidence/reviews do not expire; scope leases are separate")
        if value.decay.get("kind") != "none" or not value.decay.get("reason"):
            raise ValueError("Historical provenance requires an explicit non-decay reason")
        return value


class KnowledgeCandidate(HistoricalRecord):
    """Ordinary candidate input cannot set states, weights, or confidence."""

    id: Text
    scope: KnowledgeScope
    proposition: Proposition
    origin_refs: tuple[RecordRef, ...] = Field(min_length=1)

    @field_validator("origin_refs")
    @classmethod
    def distinct_origins(cls, value):
        _unique_refs(value)
        return value


class ClaimSnapshot(KnowledgeCandidate):
    """Resolved canonical read shape; never an ordinary create/update payload."""

    record_version: Revision
    claim_revision: Revision
    evidence_set_revision: Revision
    knowledge_state: KnowledgeState
    assessment: Assessment
    counterevidence_status: CounterevidenceStatus
    source_independence: IndependenceStatus
    latest_review_ref: RecordRef | None = None
    withdrawal_review_ref: RecordRef | None = None
    review_after: str | None = None

    @model_validator(mode="after")
    def target_consistency(self):
        _world_assessment(self.assessment, self.proposition.text)
        if self.review_after is not None:
            timestamp(self.review_after)
        if self.knowledge_state == "established" and self.latest_review_ref is None:
            raise ValueError("Established knowledge requires a review reference")
        return self

    def target(self) -> ClaimTarget:
        return ClaimTarget(claim_id=self.id, claim_revision=self.claim_revision,
                           evidence_set_revision=self.evidence_set_revision)


class EvidenceSnapshot(HistoricalRecord):
    ref: RecordRef
    source_ref: RecordRef
    scope: KnowledgeScope
    target: ClaimTarget
    relation: Literal["supports", "refutes", "qualifies", "derived_from"]
    content_check: Literal["unreviewed", "matched", "uncertain", "mismatched"]
    source_kind: Literal["primary_report", "direct_observation", "checked_derivation", "secondary", "model_output"]
    origin_ids: tuple[Text, ...] = ()
    origin_status: Literal["known", "unknown"] = "unknown"
    source_actor_id: Text | None = None
    source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    excerpt: Text
    verification_refs: tuple[RecordRef, ...] = ()

    @model_validator(mode="after")
    def origins(self):
        if self.origin_status == "known" and not self.origin_ids:
            raise ValueError("Known origins require at least one root observation/source")
        if len(set(self.origin_ids)) != len(self.origin_ids):
            raise ValueError("Duplicate origin identifiers")
        _unique_refs(self.verification_refs)
        return self


class IndependencePair(KnowledgeContract):
    evidence_ids: tuple[Text, Text]
    status: IndependenceStatus
    reason: Text

    @model_validator(mode="after")
    def two_sources(self):
        if self.evidence_ids[0] == self.evidence_ids[1]:
            raise ValueError("A record is not independent of itself")
        return self


class SourceIndependence(HistoricalRecord):
    status: IndependenceStatus
    pairs: tuple[IndependencePair, ...] = ()

    @model_validator(mode="after")
    def pair_identity(self):
        keys = [frozenset(pair.evidence_ids) for pair in self.pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate independence pair")
        if self.provenance.confidence_target != "source_independence":
            raise ValueError("Independence confidence must name its own target")
        if self.status in {"independent", "partially_independent"} and not any(
            pair.status == "independent" for pair in self.pairs
        ):
            raise ValueError("Independent corroboration needs an explicit reviewed pair")
        if self.status in {"unknown", "dependent"} and any(pair.status == "independent" for pair in self.pairs):
            raise ValueError("Aggregate status contradicts an independent pair")
        return self


class CounterevidenceSearch(HistoricalRecord):
    ref: RecordRef
    target: ClaimTarget
    scope: KnowledgeScope
    status: SearchStatus
    search_scope: Text
    planned_queries: tuple[Text, ...] = ()
    executed_queries: tuple[Text, ...] = ()
    result_refs: tuple[RecordRef, ...] = ()
    executor_version: Text
    coverage: str = ""
    limitations: tuple[Text, ...] = ()
    stop_reason: Text | None = None
    completed_at: str | None = None

    @model_validator(mode="after")
    def execution_is_not_outcome(self):
        _unique_refs(self.result_refs)
        if self.status == "completed":
            if not self.executed_queries or not self.coverage.strip() or self.completed_at is None:
                raise ValueError("Completed search requires execution, coverage, and completion time")
        elif self.completed_at is not None:
            raise ValueError("Incomplete search cannot have a completion time")
        if self.status in {"blocked", "deferred"} and not self.stop_reason:
            raise ValueError("A blocked/deferred search must preserve its stopping reason")
        if self.status in {"not_started", "planned"} and (self.executed_queries or self.result_refs):
            raise ValueError("Unstarted search cannot claim observations")
        if self.completed_at is not None and timestamp(self.completed_at) < timestamp(self.provenance.created_at):
            raise ValueError("Search completion precedes its creation")
        return self


class CounterevidenceReview(HistoricalRecord):
    status: CounterevidenceStatus
    searches: tuple[CounterevidenceSearch, ...] = ()
    evidence_refs: tuple[RecordRef, ...] = ()
    resolution: Text | None = None

    @model_validator(mode="after")
    def no_absence_without_search(self):
        _unique_refs(search.ref for search in self.searches)
        _unique_refs(self.evidence_refs)
        if self.status in {"none_found_in_scope", "addressed"} and not any(
            search.status == "completed" for search in self.searches
        ):
            raise ValueError("Absence/resolution requires a completed scoped search")
        if self.status in {"potential", "material_unresolved", "addressed"} and not self.evidence_refs:
            raise ValueError("Counterevidence findings must name evidence")
        if self.status == "addressed" and not self.resolution:
            raise ValueError("Addressed counterevidence requires a resolution")
        return self


class KnowledgeReview(HistoricalRecord):
    ref: RecordRef
    target: ClaimTarget
    scope: KnowledgeScope
    action: Literal["hold", "adopt", "dispute", "propose_withdrawal", "withdraw"]
    state_after: KnowledgeState
    profile: EvidenceProfile
    assessment: Assessment
    evidence: tuple[EvidenceSnapshot, ...]
    counterevidence: CounterevidenceReview
    source_independence: SourceIndependence
    policy_version: Literal["knowledge.k0.v1"] = POLICY_VERSION
    review_after: str
    reopens_withdrawal_ref: RecordRef | None = None

    @model_validator(mode="after")
    def review_bindings(self):
        expected_state = {"hold": "provisional", "adopt": "established", "dispute": "disputed",
                          "propose_withdrawal": "withdrawal_candidate", "withdraw": "withdrawal_candidate"}
        if self.state_after != expected_state[self.action]:
            raise ValueError("Review action and knowledge state disagree")
        if self.reopens_withdrawal_ref is not None and self.action != "adopt":
            raise ValueError("Only an explicit adopt review can reopen a withdrawal")
        if timestamp(self.review_after) <= timestamp(self.provenance.created_at):
            raise ValueError("Review due time must follow its creation")
        if self.assessment.target != "world_claim":
            raise ValueError("Knowledge reviews assess the world claim, not source/report confidence")
        _unique_refs(evidence.ref for evidence in self.evidence)
        evidence_by_id = {evidence.ref.id: evidence for evidence in self.evidence}
        if len(set(self.assessment.evidence_refs)) != len(self.assessment.evidence_refs):
            raise ValueError("Duplicate assessment evidence")
        if not set(self.assessment.evidence_refs) <= evidence_by_id.keys():
            raise ValueError("Assessment evidence is outside the reviewed snapshot")
        for evidence in self.evidence:
            if evidence.target != self.target or evidence.scope != self.scope:
                raise ValueError("Evidence must match the claim revision and authorized scope")
        for ref in self.counterevidence.evidence_refs:
            if ref.id not in evidence_by_id or evidence_by_id[ref.id].ref != ref:
                raise ValueError("Counterevidence reference is missing or stale")
            if evidence_by_id[ref.id].relation not in {"refutes", "qualifies"}:
                raise ValueError("Counterevidence findings cannot point to support/derivation alone")
        for search in self.counterevidence.searches:
            if search.target != self.target or search.scope != self.scope:
                raise ValueError("Search is for a different claim revision or scope")
        for pair in self.source_independence.pairs:
            if not set(pair.evidence_ids) <= evidence_by_id.keys():
                raise ValueError("Independence pair references absent evidence")
            left, right = (evidence_by_id[key] for key in pair.evidence_ids)
            if pair.status == "independent" and (
                left.origin_status != "known" or right.origin_status != "known"
                or set(left.origin_ids) & set(right.origin_ids)
                or left.source_ref.id == right.source_ref.id
            ):
                raise ValueError("Unknown/shared origins cannot be certified as independent")
        if self.counterevidence.status == "none_found_in_scope" and any(
            item.relation in {"refutes", "qualifies"} and item.content_check == "matched" for item in self.evidence
        ):
            raise ValueError("Matched refuting evidence contradicts a no-counterevidence finding")
        return self


class ReviewContext(KnowledgeContract):
    """Trusted resolver output, NOT client assertions or a new API payload.

    K1/K3 must derive authorization, branch eligibility, and reference status
    from canonical records. Provider-internal utilization remains out of scope.
    """

    scope: KnowledgeScope
    evaluated_at: str
    review_authorized: bool = Field(strict=True)
    path_eligible: bool = Field(strict=True)
    references_resolvable: bool = Field(strict=True)
    withdrawal_ref: RecordRef | None = None
    withdrawal_created_at: str | None = None

    @field_validator("evaluated_at")
    @classmethod
    def has_timezone(cls, value):
        timestamp(value)
        return value

    @model_validator(mode="after")
    def withdrawal_snapshot(self):
        if (self.withdrawal_ref is None) != (self.withdrawal_created_at is None):
            raise ValueError("Withdrawal identity and recorded time must be resolved together")
        if self.withdrawal_created_at is not None:
            if timestamp(self.withdrawal_created_at) > timestamp(self.evaluated_at):
                raise ValueError("A future withdrawal cannot be the current canonical withdrawal")
        return self


class AdoptionContext(ReviewContext):
    applicable_at: str
    conditions_satisfied: bool = Field(strict=True)

    @field_validator("applicable_at")
    @classmethod
    def application_timezone(cls, value):
        timestamp(value)
        return value


class AdoptionDecision(KnowledgeContract):
    eligible: bool
    reasons: tuple[str, ...]


def initial_claim(candidate: KnowledgeCandidate) -> ClaimSnapshot:
    """Pure creation projection; no persistence, evidence review, or promotion."""
    candidate = KnowledgeCandidate.model_validate(candidate.model_dump())
    return ClaimSnapshot(**candidate.model_dump(), record_version=1, claim_revision=1,
        evidence_set_revision=1, knowledge_state="provisional",
        assessment=Assessment(target="world_claim", claim=candidate.proposition.text),
        counterevidence_status="unassessed", source_independence="unknown")


def review_decision(claim: ClaimSnapshot, review: KnowledgeReview, context: ReviewContext) -> AdoptionDecision:
    """Validate review in the proposition's declared scope, not a user's query.

    A true result means the declared K0 evidence contract is satisfied. Semantic
    matching, independent origins, review authorization and source resolution
    still need trustworthy implementations; this function does not supply them.
    """
    # Revalidate nested mutable Assessment/Provenance values at the boundary.
    claim = ClaimSnapshot.model_validate(claim.model_dump())
    review = KnowledgeReview.model_validate(review.model_dump())
    context = ReviewContext.model_validate(context.model_dump())
    reasons = []
    def require(condition, reason):
        if not condition:
            reasons.append(reason)

    require(context.review_authorized, "review_not_authorized")
    require(context.path_eligible, "off_path")
    require(context.references_resolvable, "references_unresolved")
    require(claim.scope == review.scope == context.scope, "scope_mismatch")
    require(review.target == claim.target(), "stale_review_target")
    require(claim.latest_review_ref == review.ref, "review_not_current")
    require(claim.knowledge_state == review.state_after == "established" and review.action == "adopt", "not_adopted")
    require(claim.assessment == review.assessment, "assessment_snapshot_mismatch")
    require(review.assessment.claim == claim.proposition.text, "assessment_claim_mismatch")
    require(review.assessment.status == "assessed", "world_claim_unassessed")
    require(claim.counterevidence_status == review.counterevidence.status
            and claim.source_independence == review.source_independence.status, "review_metadata_mismatch")
    require(claim.review_after == review.review_after, "review_due_mismatch")
    require(claim.withdrawal_review_ref == context.withdrawal_ref, "withdrawal_snapshot_mismatch")
    require(context.withdrawal_ref is None or review.reopens_withdrawal_ref == context.withdrawal_ref,
            "withdrawal_not_reopened")
    require(context.withdrawal_ref is not None or review.reopens_withdrawal_ref is None, "unknown_withdrawal")
    now = timestamp(context.evaluated_at)
    reviewed_at = timestamp(review.provenance.created_at)
    require(reviewed_at <= now < timestamp(review.review_after), "review_outside_valid_time")
    require(context.withdrawal_created_at is None or timestamp(context.withdrawal_created_at) <= reviewed_at,
            "adoption_predates_withdrawal")
    require(timestamp(claim.provenance.created_at) <= reviewed_at, "review_predates_claim")
    require(all(timestamp(item.provenance.created_at) <= reviewed_at for item in review.evidence),
            "review_predates_evidence")
    require(timestamp(review.counterevidence.provenance.created_at) <= reviewed_at
            and timestamp(review.source_independence.provenance.created_at) <= reviewed_at, "review_predates_checks")
    proposition = claim.proposition
    require(all((proposition.subject, proposition.predicate, proposition.object))
            and proposition.polarity != "unknown" and proposition.quantifier != "unknown", "proposition_unresolved")
    require(review.counterevidence.status in {"none_found_in_scope", "addressed"}, "counterevidence_unresolved")
    require(any(search.status == "completed" for search in review.counterevidence.searches), "countersearch_incomplete")
    require(all(search.status == "completed" for search in review.counterevidence.searches), "countersearch_pending")
    require(all(timestamp(search.completed_at) <= timestamp(review.provenance.created_at)
                for search in review.counterevidence.searches if search.completed_at), "review_predates_search")
    counteritems = [item for item in review.evidence if item.relation in {"refutes", "qualifies"}]
    require(not any(item.content_check in {"unreviewed", "uncertain"} for item in counteritems), "counterevidence_pending")
    matched_counter_ids = {item.ref.id for item in counteritems if item.content_check == "matched"}
    if matched_counter_ids:
        require(review.counterevidence.status == "addressed"
                and matched_counter_ids <= {ref.id for ref in review.counterevidence.evidence_refs}, "counterevidence_not_addressed")
    supports = [item for item in review.evidence if item.relation == "supports"
                and item.content_check == "matched" and item.ref.id in review.assessment.evidence_refs]
    require(bool(supports), "support_missing")
    if review.profile == "independent_observations":
        primary = [item for item in supports if item.source_kind == "direct_observation" and item.origin_status == "known"]
        independent = {frozenset(pair.evidence_ids) for pair in review.source_independence.pairs if pair.status == "independent"}
        corroborated = any(frozenset((left.ref.id, right.ref.id)) in independent for left, right in combinations(primary, 2))
        require(proposition.kind == "empirical" and proposition.quantifier in {"single", "bounded"}, "profile_scope_mismatch")
        require(corroborated, "independent_support_missing")
    elif review.profile == "attributed_report":
        require(proposition.kind == "attributed_report" and proposition.quantifier == "single", "profile_scope_mismatch")
        require(any(item.source_kind == "primary_report" and item.source_actor_id == proposition.attribution_actor_id for item in supports),
                "attributed_primary_report_missing")
    elif review.profile == "direct_observation":
        require(proposition.kind == "bounded_observation" and proposition.quantifier == "single", "profile_scope_mismatch")
        require(any(item.source_kind == "direct_observation" and item.origin_status == "known" for item in supports),
                "direct_observation_missing")
    elif review.profile == "formal_derivation":
        require(proposition.kind == "formal", "profile_scope_mismatch")
        require(any(item.source_kind == "checked_derivation" and item.verification_refs for item in supports),
                "checked_derivation_missing")
    return AdoptionDecision(eligible=not reasons, reasons=tuple(reasons))


def adoption_decision(claim: ClaimSnapshot, review: KnowledgeReview, context: AdoptionContext) -> AdoptionDecision:
    """Query use additionally requires resolved conditions and applicability time."""
    context = AdoptionContext.model_validate(context.model_dump())
    base_context = ReviewContext.model_validate(context.model_dump(exclude={"applicable_at", "conditions_satisfied"}))
    decision = review_decision(claim, review, base_context)
    reasons = list(decision.reasons)
    if not context.conditions_satisfied:
        reasons.append("conditions_not_satisfied")
    at = timestamp(context.applicable_at)
    proposition = claim.proposition
    if proposition.valid_from is not None and timestamp(proposition.valid_from) > at:
        reasons.append("not_yet_applicable")
    if proposition.valid_until is not None and timestamp(proposition.valid_until) <= at:
        reasons.append("no_longer_applicable")
    return AdoptionDecision(eligible=not reasons, reasons=tuple(reasons))


def _unique_refs(refs):
    refs = tuple(refs)
    if len({ref.id for ref in refs}) != len(refs):
        raise ValueError("Duplicate identities/versions within a snapshot")


def _world_assessment(assessment: Assessment, text: str):
    if assessment.target != "world_claim" or assessment.claim != text:
        raise ValueError("Assessment must target this exact world claim")

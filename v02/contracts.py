from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timezone is required")
    return result.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def uid(kind: str, *parts: Any) -> str:
    return kind + "_" + digest(parts)[:32]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Provenance(Contract):
    source: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    confidence: Score | None = None
    confidence_target: str
    created_at: str = Field(default_factory=now)
    decay: dict = Field(default_factory=lambda: {"kind": "none", "reason": "historical_record"})
    expiry: str | None = None
    policy_version: str
    cause_refs: list[str] = Field(default_factory=list)


def provenance(source: str, reason: str, *, target="operation", confidence=None,
               expiry=None, causes=(), policy="canonical.v0.2") -> dict:
    return Provenance(source=source, reason=reason, confidence=confidence,
                      confidence_target=target, expiry=expiry, policy_version=policy,
                      cause_refs=list(causes)).model_dump()


class Assessment(Contract):
    target: Literal["report_fidelity", "world_claim", "source_reliability"] = "report_fidelity"
    claim: str | None = None
    score: Score | None = None
    status: Literal["unknown", "assessed", "disputed", "legacy_unassessed"] = "unknown"
    calibrated: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    policy_version: str = "evidence-only.v0.2"

    @model_validator(mode="after")
    def consistent(self):
        if self.status in {"unknown", "legacy_unassessed"} and self.score is not None:
            raise ValueError("Unknown assessments have no numeric confidence")
        if self.status == "assessed" and (self.score is None or not self.evidence_refs):
            raise ValueError("An assessment requires a score and evidence")
        if self.target == "world_claim" and not self.claim:
            raise ValueError("World confidence requires an explicit claim")
        return self


class Scope(Contract):
    conversation_id: str = Field(min_length=1)
    context_key: str = "conversation"
    sandbox_id: str | None = None


class MemoryInput(Contract):
    id: str | None = None
    conversation_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    visibility: Literal["principal", "conversation"] = "principal"
    title: str = ""
    memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] = "episodic"
    node_role: Literal["episode", "abstraction", "legacy_unknown"] = "episode"
    keywords: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    source_memory_ids: list[str] = Field(default_factory=list)
    retrieval_weight: Score = 0.5
    salience: Score = 0.5
    stability: Score = 0.1
    epistemic_status: Literal["observed", "inferred", "confirmed", "disputed"] = "inferred"
    assessment: Assessment = Field(default_factory=Assessment)
    event_time: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    usage_scope: Literal["episode_only", "explicit_instruction", "fiction"] = "episode_only"
    sandbox_id: str | None = None
    origin: Literal["automatic", "explicit", "reviewed", "migration", "nightly"] = "explicit"

    @model_validator(mode="after")
    def times_and_scope(self):
        for key in ("event_time", "valid_from", "valid_until"):
            if getattr(self, key):
                timestamp(getattr(self, key))
        if self.valid_from and self.valid_until and timestamp(self.valid_from) > timestamp(self.valid_until):
            raise ValueError("Invalid validity window")
        if (self.usage_scope == "fiction") != bool(self.sandbox_id):
            raise ValueError("Fiction requires a sandbox scope and vice versa")
        return self


class EventInput(Contract):
    id: str = Field(min_length=1)
    role: Literal["user", "assistant", "tool", "system"]
    content: str = Field(min_length=1)
    actor_id: str | None = None
    logical_actor_id: str | None = None
    model_provenance: dict = Field(default_factory=dict)
    event_time: str | None = None
    source_message_id: str | None = None
    sandbox_id: str | None = None
    context_key: str = "conversation"
    room_id: str | None = None
    session_id: str | None = None


class SyncInput(Contract):
    conversation_id: str = Field(min_length=1)
    messages: list[EventInput]
    active_event_ids: list[str]
    revision_key: str = Field(min_length=1)
    expected_revision: str | None  # Required, null only for a new conversation.
    auto_capture: bool = True


class Command(Contract):
    idempotency_key: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class MemoryCreate(Command):
    memory: MemoryInput


class MemoryEdit(Command):
    expected_version: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1)
    title: str | None = None
    keywords: list[str] | None = None


class WeightUpdate(Command):
    expected_version: int = Field(ge=1)
    retrieval_weight: Score | None = None
    salience: Score | None = None
    stability: Score | None = None


class AssessmentUpdate(Command):
    expected_version: int = Field(ge=1)
    assessment: Assessment


class KnowledgeQueryContext(Contract):
    # Conditions are resolved by a separate trusted policy, never a caller bool.
    applicable_at: str | None = None

    @model_validator(mode="after")
    def valid_time(self):
        if self.applicable_at is not None:
            timestamp(self.applicable_at)
        return self


class RunInput(Contract):
    conversation_id: str
    path_revision: str
    query: str
    idempotency_key: str
    temporal_scope: Literal["current", "historical", "future", "all"] = "current"
    sandbox_id: str | None = None
    context_key: str = "conversation"
    graph_hops: int = Field(default=0, ge=0, le=3)
    graph_node_budget: int = Field(default=100, ge=1, le=10000)
    knowledge_query: KnowledgeQueryContext = Field(default_factory=KnowledgeQueryContext)
    attention_proposal: dict | None = None
    episode_recall: Literal["auto", "episodic", "structured", "raw", "artifact"] = "auto"
    episode_target: dict | None = None


class AttemptInput(Contract):
    id: str
    run_id: str
    path_revision: str
    model_id: str
    packet_digest: str


class ObservationInput(Contract):
    stage: Literal["host_finalized", "dispatch"]
    status: Literal["retained", "not_present", "empty_context", "blocked_capacity",
                    "started", "completed", "failed", "unknown"]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_digest: str | None = None
    path_revision: str
    metrics: dict = Field(default_factory=dict)


class ResponseInput(Contract):
    response_event_id: str
    path_revision: str


class StateInput(Command):
    expected_version: int | None = Field(default=None, ge=1)
    scope: Scope
    expiry: str
    values: dict[str, Score]

    @model_validator(mode="after")
    def future_expiry(self):
        if timestamp(self.expiry) <= timestamp(now()):
            raise ValueError("Expiry must be in the future")
        return self


class SandboxInput(Command):
    conversation_id: str
    sandbox_id: str
    expected_version: int | None = Field(default=None, ge=1)
    expiry: str
    enabled: bool
    safe_as_fiction: bool | None
    current_real_world_action: bool | None
    current_loss_possible: bool | None
    urgent: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    loss_magnitude: Score = 0
    controllability: Score = 1
    reversibility: Score = 1
    cognitive_influence: Score = 0

    @model_validator(mode="after")
    def consistent_boundary(self):
        if timestamp(self.expiry) <= timestamp(now()):
            raise ValueError("Expiry must be in the future")
        if self.safe_as_fiction and self.current_real_world_action and self.current_loss_possible:
            raise ValueError("Resolve contradictory fiction/current-real-loss assessments before enabling expression")
        return self


class EdgeInput(Command):
    from_id: str
    to_id: str
    relation: Literal["co_occurrence", "temporal_next", "semantic_related", "co_recall"]
    evidence_refs: list[str] = Field(min_length=1)
    run_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


class UnresolvedInput(Command):
    conversation_id: str
    expected_version: int | None = Field(default=None, ge=1)
    status: Literal["active", "blocked", "deferred", "resolved", "abandoned"]
    description: str
    expected_information_gain: Score | None = None
    resume_conditions: str = ""

"""K1 command inputs. Identity, source snapshots, clocks and contexts are server-owned."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Assessment, Command, Contract
from .knowledge_contracts import (CounterevidenceStatus, EvidenceProfile, IndependencePair,
    IndependenceStatus, Proposition, RecordRef, Revision, SearchStatus, Text)


class InputScope(Contract):
    conversation_id: Text
    context_key: Text = "conversation"
    sandbox_id: Text | None = None


class KnowledgeCommand(Command):
    path_revision: Text


class SourceCreate(KnowledgeCommand):
    scope: InputScope
    event_ref: RecordRef
    parent_source_refs: tuple[RecordRef, ...] = ()


class ClaimCreate(KnowledgeCommand):
    scope: InputScope
    proposition: Proposition
    origin_refs: tuple[RecordRef, ...] = Field(min_length=1)


class ClaimCommand(KnowledgeCommand):
    expected_version: Revision
    claim_revision: Revision
    evidence_set_revision: Revision


class ClaimEdit(ClaimCommand):
    proposition: Proposition


class EvidenceInput(Contract):
    source_ref: RecordRef
    relation: Literal["supports", "refutes", "qualifies", "derived_from"]
    content_check: Literal["unreviewed", "matched", "uncertain", "mismatched"]
    source_kind: Literal["primary_report", "direct_observation", "checked_derivation", "secondary", "model_output"]
    origin_status: Literal["known", "unknown"] = "unknown"
    span_start: int = Field(ge=0, strict=True)
    span_end: int = Field(ge=1, strict=True)
    verification_refs: tuple[RecordRef, ...] = ()
    review_reason: Text

    @model_validator(mode="after")
    def nonempty_span(self):
        if self.span_start >= self.span_end:
            raise ValueError("Evidence spans must be nonempty")
        return self


class EvidenceReplace(ClaimCommand):
    evidence: tuple[EvidenceInput, ...]


class SearchRecord(ClaimCommand):
    status: SearchStatus
    search_scope: Text
    planned_queries: tuple[Text, ...] = ()
    executed_queries: tuple[Text, ...] = ()
    result_refs: tuple[RecordRef, ...] = ()
    executor_version: Text
    coverage: str = ""
    limitations: tuple[Text, ...] = ()
    stop_reason: Text | None = None


class ClaimReview(ClaimCommand):
    action: Literal["hold", "adopt", "dispute", "propose_withdrawal", "withdraw"]
    profile: EvidenceProfile
    assessment: Assessment
    search_refs: tuple[RecordRef, ...] = ()
    counterevidence_status: CounterevidenceStatus
    counterevidence_refs: tuple[RecordRef, ...] = ()
    counterevidence_resolution: Text | None = None
    source_independence: IndependenceStatus
    independence_pairs: tuple[IndependencePair, ...] = ()
    review_after: str
    reopens_withdrawal_ref: RecordRef | None = None

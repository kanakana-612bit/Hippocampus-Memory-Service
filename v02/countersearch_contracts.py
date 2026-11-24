"""K3 bounded internal countersearch contracts; lexical hits are not evidence judgments."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Score, timestamp
from .knowledge_commands import ClaimCommand, KnowledgeCommand
from .knowledge_contracts import (
    ClaimTarget, HistoricalRecord, KnowledgeContract, KnowledgeScope, RecordRef, Text,
)


COUNTERSEARCH_POLICY = "knowledge.countersearch.k3.v1"
EXECUTOR_VERSION = "internal_corpus.lexical.v1"
SearchTerm = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"\S")]
QueryPurpose = Literal["falsification", "alternative_explanation", "boundary_case", "source_correction"]
QUERY_PURPOSES = frozenset({"falsification", "alternative_explanation", "boundary_case", "source_correction"})


class CountersearchQuery(KnowledgeContract):
    purpose: QueryPurpose
    text: Text = Field(max_length=2000)
    terms: tuple[SearchTerm, ...] = Field(default=(), max_length=16)
    expected_information_gain: Score = 0.5
    reason: Text

    @model_validator(mode="after")
    def unique_terms(self):
        if len({term.casefold() for term in self.terms}) != len(self.terms):
            raise ValueError("Repeated lexical query term")
        return self


class CountersearchBudgets(KnowledgeContract):
    query_limit: int = Field(default=16, ge=1, le=16, strict=True)
    source_limit: int = Field(default=128, ge=1, le=2000, strict=True)
    result_limit: int = Field(default=32, ge=1, le=256, strict=True)
    time_ms: int = Field(default=1000, ge=1, le=30000, strict=True)
    min_expected_information_gain: Score = 0.05


class SearchPlanCreate(ClaimCommand):
    queries: tuple[CountersearchQuery, ...] = Field(min_length=1, max_length=16)
    not_applicable_reasons: dict[QueryPurpose, Text] = Field(default_factory=dict)
    budgets: CountersearchBudgets = Field(default_factory=CountersearchBudgets)
    lease_seconds: int = Field(default=60, ge=1, le=300, strict=True)

    @model_validator(mode="after")
    def purposes_are_explicit(self):
        active = {query.purpose for query in self.queries}
        excluded = set(self.not_applicable_reasons)
        if active & excluded or active | excluded != QUERY_PURPOSES:
            raise ValueError("Each countersearch purpose requires a query or a distinct not-applicable reason")
        return self


class SearchExecute(KnowledgeCommand):
    """The server resolves all plan, target, lease and executor state."""


class CorpusSource(KnowledgeContract):
    ref: RecordRef
    source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SearchPlanSnapshot(HistoricalRecord):
    ref: RecordRef
    claim_ref: RecordRef
    target: ClaimTarget
    scope: KnowledgeScope
    path_revision: Text
    queries: tuple[CountersearchQuery, ...] = Field(min_length=1, max_length=16)
    not_applicable_reasons: dict[QueryPurpose, Text]
    budgets: CountersearchBudgets
    lease_seconds: int = Field(ge=1, le=300, strict=True)
    corpus: tuple[CorpusSource, ...]
    excluded_source_count: int = Field(ge=0)
    executor_version: Literal["internal_corpus.lexical.v1"] = EXECUTOR_VERSION
    policy_version: Literal["knowledge.countersearch.k3.v1"] = COUNTERSEARCH_POLICY

    @model_validator(mode="after")
    def unique_corpus(self):
        if len({item.ref.id for item in self.corpus}) != len(self.corpus):
            raise ValueError("Repeated corpus source")
        if self.target.claim_id != self.claim_ref.id:
            raise ValueError("Plan claim identity does not match its target")
        return self


class SearchAttemptSnapshot(HistoricalRecord):
    ref: RecordRef
    plan_ref: RecordRef
    target: ClaimTarget
    scope: KnowledgeScope
    path_revision: Text
    host_id: Text
    status: Literal["completed", "blocked", "deferred"]
    started_at: str
    finished_at: str
    stop_reason: Text | None = None
    search_ref: RecordRef | None = None
    result_refs: tuple[RecordRef, ...] = ()
    executed_query_indices: tuple[int, ...] = ()
    query_results: tuple[dict, ...] = ()
    coverage: dict
    limitations: tuple[Text, ...]
    policy_version: Literal["knowledge.countersearch.k3.v1"] = COUNTERSEARCH_POLICY

    @model_validator(mode="after")
    def coherent_attempt(self):
        if timestamp(self.finished_at) < timestamp(self.started_at):
            raise ValueError("Attempt completion precedes acquisition")
        if self.status != "completed" and not self.stop_reason:
            raise ValueError("An incomplete attempt requires a stopping reason")
        if len({item.id for item in self.result_refs}) != len(self.result_refs):
            raise ValueError("Duplicate attempt result")
        return self

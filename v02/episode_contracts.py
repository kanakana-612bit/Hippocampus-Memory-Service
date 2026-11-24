"""Strict proposals for derived episodic memory; source facts stay immutable.

These are declarations, not authorization or evidence verification. The service
resolves every versioned reference, source digest, quote, scope and DAG edge in
one transaction. A model cannot grant confidence or confirm a user's position.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from .contracts import Command, Contract, Scope
from .knowledge_contracts import RecordRef


POLICY_VERSION = "episode.multiresolution.v1"
Text = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
EpisodeLevel = Literal["local", "multi_day", "theme"]
StructuredKind = Literal["user_decision", "user_stance", "user_intent", "unresolved"]


class EpisodeContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


def raw_text_digest(text: str) -> str:
    """Digest unnormalized UTF-8 source text, not a JSON-encoded string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique_refs(refs):
    keys = [(ref.id, ref.version) for ref in refs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate versioned references")


class SourceSpan(EpisodeContract):
    event_ref: RecordRef
    source_digest: Digest
    start: Annotated[int, Field(strict=True, ge=0)]
    end: PositiveInt
    quote: Text

    @model_validator(mode="after")
    def unicode_range(self):
        if self.end <= self.start or len(self.quote) != self.end - self.start:
            raise ValueError("Source spans use nonempty half-open Unicode codepoint ranges")
        return self


class GroundedItem(EpisodeContract):
    text: Text
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)


class GeneratorMetadata(EpisodeContract):
    model_id: Text
    prompt_version: Text
    algorithm_version: Text


class ChunkPolicy(EpisodeContract):
    inactivity_seconds: PositiveInt = 1800
    max_events: PositiveInt | None = None
    max_characters: PositiveInt | None = None


class ChunkSeal(Command):
    scope: Scope
    path_revision: Text
    response_in_progress: bool = False
    policy: ChunkPolicy = Field(default_factory=ChunkPolicy)


class SummaryDraft(EpisodeContract):
    chunk_ref: RecordRef
    supersedes_ref: RecordRef | None = None
    summary: Text
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    topics: tuple[Text, ...] = ()
    events: tuple[GroundedItem, ...] = ()
    decision_candidates: tuple[GroundedItem, ...] = ()
    stance_candidates: tuple[GroundedItem, ...] = ()
    intent_candidates: tuple[GroundedItem, ...] = ()
    unresolved_candidates: tuple[GroundedItem, ...] = ()
    related_entities: tuple[GroundedItem, ...] = ()


# Descriptive name retained for generation callers; there is one schema.
ProvisionalSummaryDraft = SummaryDraft


class EpisodeRelation(EpisodeContract):
    kind: Literal["continuation", "reconnect"]
    target_ref: RecordRef


class EpisodeDraft(EpisodeContract):
    local_id: Text
    level: EpisodeLevel
    title: Text
    summary: Text
    source_spans: tuple[SourceSpan, ...] = ()
    child_refs: tuple[RecordRef, ...] = ()
    child_local_ids: tuple[Text, ...] = ()
    relations: tuple[EpisodeRelation, ...] = ()
    supersedes_ref: RecordRef | None = None

    @model_validator(mode="after")
    def grounded_graph(self):
        if not (self.source_spans or self.child_refs or self.child_local_ids):
            raise ValueError("An episode needs original spans or grounded child episodes")
        _unique_refs(self.child_refs)
        if len(set(self.child_local_ids)) != len(self.child_local_ids):
            raise ValueError("Duplicate local children")
        if self.local_id in self.child_local_ids:
            raise ValueError("An episode cannot contain itself")
        relations = [(r.kind, r.target_ref.id, r.target_ref.version) for r in self.relations]
        if len(set(relations)) != len(relations):
            raise ValueError("Duplicate episode relations")
        return self


class StructuredCandidate(EpisodeContract):
    local_id: Text | None = None
    kind: StructuredKind
    subject: Text
    topic: Text
    text: Text
    status: Literal["candidate"] = "candidate"
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    episode_refs: tuple[RecordRef, ...] = ()
    episode_local_ids: tuple[Text, ...] = ()
    supersedes_ref: RecordRef | None = None

    @model_validator(mode="after")
    def references(self):
        _unique_refs(self.episode_refs)
        if len(set(self.episode_local_ids)) != len(self.episode_local_ids):
            raise ValueError("Duplicate local episode references")
        return self


class EpisodeProposal(EpisodeContract):
    summaries: tuple[SummaryDraft, ...] = ()
    episodes: tuple[EpisodeDraft, ...] = ()
    structured_candidates: tuple[StructuredCandidate, ...] = ()

    @model_validator(mode="after")
    def local_dag(self):
        _unique_refs(summary.chunk_ref for summary in self.summaries)
        by_id = {episode.local_id: episode for episode in self.episodes}
        if len(by_id) != len(self.episodes):
            raise ValueError("Duplicate episode local IDs")
        candidate_ids = [c.local_id for c in self.structured_candidates if c.local_id is not None]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("Duplicate structured candidate local IDs")
        for episode in self.episodes:
            if not set(episode.child_local_ids) <= by_id.keys():
                raise ValueError("Unknown local child")
        for candidate in self.structured_candidates:
            if not set(candidate.episode_local_ids) <= by_id.keys():
                raise ValueError("Unknown local episode reference")
        # Iterative traversal avoids recursion limits for large valid proposals.
        remaining = {key: set(value.child_local_ids) for key, value in by_id.items()}
        while remaining:
            leaves = {key for key, children in remaining.items() if not children}
            if not leaves:
                raise ValueError("Episode hierarchy contains a cycle")
            remaining = {key: children - leaves for key, children in remaining.items() if key not in leaves}
        return self


DailyOrganizationDraft = EpisodeProposal


class JobPrepare(Command):
    scope: Scope
    path_revision: Text
    operation: Literal["provisional", "daily", "regenerate"]
    day: str | None = None
    source_refs: tuple[RecordRef, ...] = ()

    @model_validator(mode="after")
    def request_bounds(self):
        if self.day is not None:
            if date.fromisoformat(self.day).isoformat() != self.day:
                raise ValueError("day must be an ISO calendar date")
        _unique_refs(self.source_refs)
        if self.operation == "regenerate" and not self.source_refs:
            raise ValueError("Regeneration requires explicit versioned source references")
        return self


class JobCommit(Command):
    manifest_digest: Digest
    proposal: EpisodeProposal
    generator: GeneratorMetadata


class SummarySubmit(Command):
    scope: Scope
    path_revision: Text
    draft: SummaryDraft
    generator: GeneratorMetadata


class EpisodeSubmit(Command):
    scope: Scope
    path_revision: Text
    draft: EpisodeDraft
    generator: GeneratorMetadata


class StructuredReview(Command):
    scope: Scope
    path_revision: Text
    target_ref: RecordRef
    action: Literal["confirm", "withdraw", "supersede"]
    replacement_ref: RecordRef | None = None
    expected_review_ref: RecordRef | None = None
    unresolved_status: Literal["active", "blocked", "deferred", "resolved", "abandoned"] | None = None

    @model_validator(mode="after")
    def replacement(self):
        if (self.action == "supersede") != (self.replacement_ref is not None):
            raise ValueError("Only supersession requires a replacement reference")
        if self.replacement_ref is not None and self.replacement_ref.id == self.target_ref.id:
            raise ValueError("A structured memory cannot supersede itself")
        if self.unresolved_status is not None and self.action != "confirm":
            raise ValueError("Only confirmation can set an unresolved item's state")
        return self


class ArtifactReferenceCreate(Command):
    scope: Scope
    path_revision: Text
    title: Text
    uri: Text
    kind: Literal["git_repository", "implementation_document", "design_document", "other"]
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    supersedes_ref: RecordRef | None = None

    @model_validator(mode="after")
    def pointer_only(self):
        # No URI is opened here. Do not persist embedded credentials as pointers.
        parsed = urlsplit(self.uri)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Artifact pointers must not embed credentials")
        return self


class EpisodeQuery(EpisodeContract):
    scope: Scope
    path_revision: Text
    query: str = ""
    mode: Literal["auto", "episodic", "structured", "raw", "artifact"] = "auto"
    level: EpisodeLevel | None = None
    target_ref: RecordRef | None = None
    include_history: bool = False
    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 20

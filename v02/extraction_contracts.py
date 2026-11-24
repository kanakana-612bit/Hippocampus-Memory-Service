"""K2 structured proposals. Extraction fidelity never certifies a world claim."""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, model_validator

from .contracts import Score, digest
from .knowledge_commands import InputScope, KnowledgeCommand
from .knowledge_contracts import (HistoricalRecord, KnowledgeContract, KnowledgeScope,
                                  Proposition, RecordRef, Text)


EXTRACTION_POLICY = "knowledge.extraction.k2.v1"
EXPLICIT_EXTRACTOR = "explicit_proposal.v1"


class ExtractionConfidence(KnowledgeContract):
    target: Literal["claim_extraction"] = "claim_extraction"
    score: Score | None = None
    calibrated: Literal[False] = False


class ExtractionSpan(KnowledgeContract):
    source_ref: RecordRef
    start: int = Field(ge=0, strict=True)
    end: int = Field(ge=1, strict=True)
    quote: Text
    unit: Literal["unicode_codepoint"] = "unicode_codepoint"

    @model_validator(mode="after")
    def nonempty(self):
        if self.start >= self.end:
            raise ValueError("Extraction spans must be nonempty half-open intervals")
        return self


class ExtractionProposal(KnowledgeContract):
    proposition: Proposition
    spans: tuple[ExtractionSpan, ...] = Field(min_length=1)
    confidence: ExtractionConfidence = Field(default_factory=ExtractionConfidence)
    reason: Text
    limitations: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def unique_spans(self):
        seen = set()
        versions = {}
        for span in self.spans:
            key = (span.source_ref.id, span.source_ref.version, span.start, span.end)
            if key in seen:
                raise ValueError("Repeated extraction span")
            seen.add(key)
            old = versions.setdefault(span.source_ref.id, span.source_ref.version)
            if old != span.source_ref.version:
                raise ValueError("One proposal cannot mix versions of the same source")
        return self

    def processing_fingerprint(self, scope: KnowledgeScope) -> str:
        # Include annotations: a changed assertion is new history, never an overwrite.
        return digest({"scope": scope.model_dump(), "proposal": self.model_dump(),
                       "extractor_version": EXPLICIT_EXTRACTOR, "policy_version": EXTRACTION_POLICY})


class ExtractionSubmit(KnowledgeCommand):
    scope: InputScope
    extractor_version: Literal["explicit_proposal.v1"] = EXPLICIT_EXTRACTOR
    proposals: tuple[ExtractionProposal, ...] = Field(min_length=1)


class ExtractionSource(KnowledgeContract):
    """Resolved input for an extractor, never supplied as a trusted API payload."""
    ref: RecordRef
    scope: KnowledgeScope
    content: Text
    source_digest: Text


class ResolvedExtractionSpan(ExtractionSpan):
    source_digest: Text
    role: Literal["user", "assistant", "tool", "system"]
    actor_id: Text | None
    model_provenance: dict
    parent_source_refs: tuple[RecordRef, ...]
    event_refs: tuple[RecordRef, ...]
    origin_ids: tuple[Text, ...]
    origin_status: Literal["unknown"] = "unknown"


class ExtractionSnapshot(HistoricalRecord):
    ref: RecordRef
    claim_ref: RecordRef
    scope: KnowledgeScope
    proposal: ExtractionProposal
    spans: tuple[ResolvedExtractionSpan, ...] = Field(min_length=1)
    processing_fingerprint: Text
    exact_fingerprint: Text
    related_claim_refs: tuple[RecordRef, ...] = ()
    path_revision: Text
    extractor_version: Literal["explicit_proposal.v1"] = EXPLICIT_EXTRACTOR
    policy_version: Literal["knowledge.extraction.k2.v1"] = EXTRACTION_POLICY
    execution_mode: Literal["explicit_structured_proposal"] = "explicit_structured_proposal"


class ProposalExtractor(Protocol):
    """Pure proposal interface; implementations cannot persist or adopt knowledge."""
    version: str

    def extract(self, sources: tuple[ExtractionSource, ...],
                proposals: tuple[ExtractionProposal, ...]) -> tuple[ExtractionProposal, ...]: ...


class ExplicitProposalExtractor:
    """Validate explicitly supplied proposals; no text inference or model call."""
    version = EXPLICIT_EXTRACTOR

    def extract(self, sources, proposals):
        resolved = {}
        for source in sources:
            source = ExtractionSource.model_validate(source)
            if digest(source.content) != source.source_digest:
                raise ValueError("Extraction source digest mismatch")
            key = (source.ref.id, source.ref.version)
            if key in resolved:
                raise ValueError("Repeated extractor source")
            resolved[key] = source
        checked = []
        for proposal in proposals:
            proposal = ExtractionProposal.model_validate(proposal)
            for span in proposal.spans:
                source = resolved.get((span.source_ref.id, span.source_ref.version))
                if source is None:
                    raise ValueError("Extraction source was not resolved")
                if span.end > len(source.content) or source.content[span.start:span.end] != span.quote:
                    raise ValueError("Extraction quote does not match the source at the declared codepoint span")
            checked.append(proposal)
        return tuple(checked)

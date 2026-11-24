"""K2 explicit proposal acceptance through the existing canonical transaction."""
from __future__ import annotations

from .contracts import Provenance, now, uid
from .extraction_contracts import (EXTRACTION_POLICY, ExplicitProposalExtractor,
    ExtractionSnapshot, ExtractionSource, ExtractionSubmit, ResolvedExtractionSpan)
from .knowledge_contracts import ClaimSnapshot, KnowledgeCandidate, RecordRef, initial_claim
from .knowledge_service import historical_view
from .storage import Missing


class ExtractionService:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.knowledge = service.knowledge
        self.extractor = ExplicitProposalExtractor()

    def submit(self, identity, value):
        self.knowledge._require(identity, "write")
        req = ExtractionSubmit.model_validate(value)
        return self.knowledge._command(identity, "extraction.submit", req,
            lambda con: self.submit_in_transaction(con, identity, req))

    def submit_in_transaction(self, con, identity, request):
        """Also used by offline migration; caller owns commit, audit receipt and rollback."""
        self.knowledge._require(identity, "write")
        req = ExtractionSubmit.model_validate(request)
        if not con.in_transaction:
            raise ValueError("Extraction requires an enclosing write transaction")
        if req.extractor_version != self.extractor.version:
            raise ValueError("Extractor version does not match the configured explicit executor")
        scope = self.knowledge._scope(identity, req.scope)
        path = self.knowledge._path(con, scope, req.path_revision)
        sources = {}
        resolved_sources = []
        for proposal in req.proposals:
            for span in proposal.spans:
                key = (span.source_ref.id, span.source_ref.version)
                if key not in sources:
                    record = self.knowledge._source(con, scope, span.source_ref, path)
                    sources[key] = record
                    resolved_sources.append(ExtractionSource(ref=span.source_ref, scope=scope,
                        content=record["data"]["content"], source_digest=record["data"]["source_digest"]))
        proposals = self.extractor.extract(tuple(resolved_sources), req.proposals)
        saved = {}
        for proposal in proposals:
            fingerprint = proposal.processing_fingerprint(scope)
            extraction_id = uid("knowledge_extraction", identity.principal, fingerprint)
            try:
                record = self.db.get(con, identity.principal, extraction_id, "knowledge_extraction")
            except Missing:
                record = None
            if record is not None:
                original = ExtractionSnapshot.model_validate(record["data"])
                if original.proposal != proposal or original.scope != scope:
                    raise ValueError("Extraction processing identity mismatch")
                # A replay identifies the original candidate revision, even after edit/withdraw.
                claim_ref = original.claim_ref
                saved[(claim_ref.id, claim_ref.version)] = {
                    "id": claim_ref.id, "version": claim_ref.version, "kind": "knowledge_claim"}
                saved[(record["id"], record["version"])] = record
                continue
            refs = {span.source_ref.id: span.source_ref for span in proposal.spans}
            related = self._related(con, scope, proposal.proposition, path)
            stamp = now()
            candidate = KnowledgeCandidate(id=uid("knowledge_claim", "extraction", identity.principal, fingerprint),
                scope=scope, proposition=proposal.proposition, origin_refs=tuple(refs[k] for k in sorted(refs)),
                provenance=Provenance(source="authorized_host:" + identity.host, reason=proposal.reason,
                    confidence=None, confidence_target="candidate_origin", created_at=stamp,
                    policy_version=EXTRACTION_POLICY, cause_refs=[extraction_id, *sorted(refs)]))
            claim = initial_claim(candidate)
            claim_record = self.db.put(con, identity=claim.id, principal=identity.principal,
                kind="knowledge_claim", conversation=scope.conversation_id, data=claim.model_dump(),
                reason="extraction_candidate_created")
            spans = []
            for span in proposal.spans:
                source = sources[(span.source_ref.id, span.source_ref.version)]["data"]
                event = self.knowledge._resolve(con, identity.principal,
                    RecordRef.model_validate(source["event_ref"]), "event")
                spans.append(ResolvedExtractionSpan(**span.model_dump(), source_digest=source["source_digest"],
                    role=source["role"], actor_id=source["actor_id"],
                    model_provenance=event["data"].get("model_provenance", {}),
                    parent_source_refs=source["parent_source_refs"], event_refs=source["event_refs"],
                    origin_ids=source["origin_ids"]))
            snapshot = ExtractionSnapshot(ref=RecordRef(id=extraction_id, version=1),
                claim_ref=self.knowledge._ref(claim_record), scope=scope, proposal=proposal,
                spans=tuple(spans), processing_fingerprint=fingerprint,
                exact_fingerprint=proposal.proposition.exact_fingerprint(scope), related_claim_refs=related,
                path_revision=req.path_revision,
                provenance=Provenance(source="authorized_host:" + identity.host, reason=proposal.reason,
                    confidence=proposal.confidence.score, confidence_target="claim_extraction", created_at=stamp,
                    policy_version=EXTRACTION_POLICY, cause_refs=[claim.id, *sorted(refs)]))
            record = self.db.put(con, identity=extraction_id, principal=identity.principal,
                kind="knowledge_extraction", conversation=scope.conversation_id, data=snapshot.model_dump(),
                reason="explicit_extraction_recorded")
            saved[(claim_record["id"], claim_record["version"])] = claim_record
            saved[(record["id"], record["version"])] = record
        return self.knowledge._ack(*saved.values())

    def _related(self, con, scope, proposition, path):
        refs = []
        fingerprint = proposition.exact_fingerprint(scope)
        for record in self.db.list(con, scope.principal_id, "knowledge_claim", scope.conversation_id):
            claim = ClaimSnapshot.model_validate(record["data"])
            if claim.scope != scope or claim.proposition.exact_fingerprint(scope) != fingerprint:
                continue
            try:
                self.knowledge._claim_origins(con, claim, path)
            except (ValueError, Missing):
                continue
            refs.append(self.knowledge._ref(record))
        # Similarity is historical linkage, never support, independence or adoption.
        return tuple(refs)

    def get(self, identity, extraction_id):
        self.knowledge._require(identity, "read")
        with self.db.connection() as con:
            return historical_view(self.db.get(con, identity.principal, extraction_id, "knowledge_extraction"))

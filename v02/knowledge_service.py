"""Canonical knowledge commands with K3 execution/inspection gates for manual review."""
from __future__ import annotations

from .contracts import Assessment, digest, now, uid
from .knowledge_commands import (ClaimCreate, ClaimEdit, ClaimReview, EvidenceReplace,
                                  SearchRecord, SourceCreate)
from .knowledge_contracts import (POLICY_VERSION, ClaimSnapshot, CounterevidenceReview,
    CounterevidenceSearch, EvidenceSnapshot, KnowledgeCandidate, KnowledgeReview,
    KnowledgeScope, Provenance, RecordRef, ReviewContext, SourceIndependence,
    initial_claim, review_decision)
from .storage import Conflict, Missing


def historical_view(record):
    """A raw record is inspectable history, never a current-use certificate."""
    if record["kind"].startswith("knowledge_"):
        return {**record, "interpretation": "historical_snapshot", "current_applicability": "not_evaluated"}
    return record


class KnowledgeService:
    def __init__(self, service):
        self.service = service
        self.db = service.db

    @staticmethod
    def _require(identity, permission):
        if not identity.principal or not identity.host or permission not in identity.permissions:
            raise PermissionError("Knowledge command permission denied")

    @staticmethod
    def _scope(identity, value):
        return KnowledgeScope(principal_id=identity.principal, conversation_id=value.conversation_id,
            context_key=value.context_key, domain="fiction" if value.sandbox_id else "world",
            sandbox_id=value.sandbox_id)

    @staticmethod
    def _provenance(identity, reason, target, causes=(), stamp=None):
        return Provenance(source="authorized_host:" + identity.host, reason=reason,
            confidence=None, confidence_target=target, created_at=stamp or now(),
            policy_version=POLICY_VERSION, cause_refs=list(causes))

    @staticmethod
    def _ref(record):
        return RecordRef(id=record["id"], version=record["version"])

    @staticmethod
    def _ack(*records):
        return {"result_semantics": "recorded_revisions_only", "current_applicability": "not_evaluated",
                "records": [{"id": r["id"], "version": r["version"], "kind": r["kind"]} for r in records]}

    def _command(self, identity, name, req, operation, claim_id=None):
        payload = {"actor_host_id": identity.host, **req.model_dump()}
        if claim_id is not None:
            payload["claim_id"] = claim_id
        return self.db.command(identity.principal, "knowledge." + name, req.idempotency_key, payload, operation)

    def _resolve(self, con, principal, ref, kind):
        record = self.db.get(con, principal, ref.id, kind)
        if record["version"] != ref.version:
            raise Conflict("Stale knowledge reference")
        return record

    def _path(self, con, scope, revision):
        return self.service.check_path(con, scope.principal_id, scope.conversation_id, revision)

    def _source(self, con, scope, ref, path=None):
        source = self._resolve(con, scope.principal_id, ref, "knowledge_source")
        data = source["data"]
        if data["scope"] != scope.model_dump() or source["conversation_id"] != scope.conversation_id:
            raise ValueError("Knowledge source is outside the claim scope")
        # Sources are immutable closures of their origin events, including parents.
        for event_ref in data["event_refs"]:
            event = self._resolve(con, scope.principal_id, RecordRef.model_validate(event_ref), "event")
            if (event["conversation_id"] != scope.conversation_id or event["data"].get("sandbox_id") != scope.sandbox_id
                    or event["data"].get("context_key", "conversation") != scope.context_key):
                raise ValueError("Source event crosses conversation, context or fiction boundary")
            if path is not None and event["id"] not in path["data"]["event_ids"]:
                raise ValueError("Source event is off the selected path")
        return source

    def _claim(self, con, identity, claim_id, req):
        record = self.db.get(con, identity.principal, claim_id, "knowledge_claim")
        claim = ClaimSnapshot.model_validate(record["data"])
        if claim.scope.principal_id != identity.principal or claim.record_version != record["version"]:
            raise Conflict("Canonical claim metadata mismatch")
        if (record["version"], claim.claim_revision, claim.evidence_set_revision) != (
                req.expected_version, req.claim_revision, req.evidence_set_revision):
            raise Conflict("Stale claim, record, or evidence-set revision")
        path = self._path(con, claim.scope, req.path_revision)
        return record, claim, path

    def _claim_origins(self, con, claim, path=None):
        return [self._source(con, claim.scope, ref, path) for ref in claim.origin_refs]

    def _put_claim(self, con, record, data, reason):
        data["record_version"] = record["version"] + 1
        claim = ClaimSnapshot.model_validate(data)
        return self.db.put(con, identity=record["id"], principal=record["principal_id"],
            kind="knowledge_claim", conversation=record["conversation_id"], data=claim.model_dump(),
            reason=reason, expected=record["version"])

    @staticmethod
    def _invalidate(claim, *, proposition=None, evidence_changed=False):
        data = claim.model_dump()
        if proposition is not None:
            data["proposition"] = proposition.model_dump()
            data["claim_revision"] += 1
        if proposition is not None or evidence_changed:
            data["evidence_set_revision"] += 1
        data.update(knowledge_state="provisional", counterevidence_status="unassessed",
                    source_independence="unknown", latest_review_ref=None, review_after=None,
                    assessment=Assessment(target="world_claim", claim=data["proposition"]["text"]).model_dump())
        # Withdrawal identity survives edits, evidence replacement, and hold.
        return data

    def create_source(self, identity, value):
        self._require(identity, "write")
        req = SourceCreate.model_validate(value)
        return self._command(identity, "source.create", req,
            lambda con: self._ack(self.register_source_in_transaction(con, identity, req)))

    def register_source_in_transaction(self, con, identity, req):
        """Shared canonical source registration for API and explicit offline migration."""
        self._require(identity, "write")
        req = SourceCreate.model_validate(req)
        if not con.in_transaction:
            raise ValueError("Source registration requires an enclosing write transaction")
        scope = self._scope(identity, req.scope)
        path = self._path(con, scope, req.path_revision)
        event = self._resolve(con, identity.principal, req.event_ref, "event")
        if (event["conversation_id"] != scope.conversation_id or event["data"].get("sandbox_id") != scope.sandbox_id
                or event["data"].get("context_key", "conversation") != scope.context_key):
            raise ValueError("Source event is outside the requested scope")
        if event["id"] not in path["data"]["event_ids"]:
            raise ValueError("Only selected events can be registered as knowledge sources")
        parents = [self._source(con, scope, ref, path) for ref in req.parent_source_refs]
        if len({r["id"] for r in parents}) != len(parents):
            raise ValueError("Repeated parent source")
        event_refs = {event["id"]: req.event_ref.model_dump()}
        for parent in parents:
            event_refs.update({ref["id"]: ref for ref in parent["data"]["event_refs"]})
        roots = sorted({root for parent in parents for root in parent["data"]["origin_ids"]}) if parents else [event["id"]]
        body = event["data"]
        identity_id = uid("knowledge_source", identity.principal, req.idempotency_key)
        data = {"scope": scope.model_dump(), "event_ref": req.event_ref.model_dump(),
            "parent_source_refs": [r.model_dump() for r in req.parent_source_refs],
            "event_refs": [event_refs[k] for k in sorted(event_refs)], "origin_ids": roots,
            "content": body["content"], "source_digest": digest(body["content"]),
            "role": body["role"], "actor_id": body.get("actor_id") or body.get("logical_actor_id"),
            "event_time": body.get("event_time"),
            "provenance": self._provenance(identity, req.reason, "source_record", event_refs).model_dump()}
        return self.db.put(con, identity=identity_id, principal=identity.principal, kind="knowledge_source",
            conversation=scope.conversation_id, data=data, reason="knowledge_source_recorded")

    def create_claim(self, identity, value):
        self._require(identity, "write")
        req = ClaimCreate.model_validate(value)
        scope = self._scope(identity, req.scope)
        def operation(con):
            path = self._path(con, scope, req.path_revision)
            for ref in req.origin_refs:
                self._source(con, scope, ref, path)
            candidate = KnowledgeCandidate(id=uid("knowledge_claim", identity.principal, req.idempotency_key),
                scope=scope, proposition=req.proposition, origin_refs=req.origin_refs,
                provenance=self._provenance(identity, req.reason, "candidate_origin", [r.id for r in req.origin_refs]))
            claim = initial_claim(candidate)
            record = self.db.put(con, identity=claim.id, principal=identity.principal, kind="knowledge_claim",
                conversation=scope.conversation_id, data=claim.model_dump(), reason="knowledge_candidate_created")
            return self._ack(record)
        return self._command(identity, "claim.create", req, operation)

    def edit_claim(self, identity, claim_id, value):
        self._require(identity, "write")
        req = ClaimEdit.model_validate(value)
        def operation(con):
            record, claim, path = self._claim(con, identity, claim_id, req)
            self._claim_origins(con, claim, path)
            if req.proposition == claim.proposition:
                raise ValueError("A proposition change is required")
            data = self._invalidate(claim, proposition=req.proposition)
            data["provenance"] = self._provenance(identity, req.reason, "claim_change", [claim.id]).model_dump()
            updated = self._put_claim(con, record, data, "knowledge_proposition_changed")
            return self._ack(updated)
        return self._command(identity, "claim.edit", req, operation, claim_id)

    def replace_evidence(self, identity, claim_id, value):
        self._require(identity, "review")
        req = EvidenceReplace.model_validate(value)
        def operation(con):
            record, claim, path = self._claim(con, identity, claim_id, req)
            self._claim_origins(con, claim, path)
            draft = ClaimSnapshot.model_validate(self._invalidate(claim, evidence_changed=True))
            evidence_records = []
            for index, item in enumerate(req.evidence):
                source = self._source(con, claim.scope, item.source_ref, path)["data"]
                if item.span_end > len(source["content"]):
                    raise ValueError("Evidence span exceeds the source snapshot")
                if source["role"] == "assistant" and item.source_kind != "model_output":
                    raise ValueError("Assistant output cannot become a primary observation/report/proof")
                if source["role"] == "system" and item.source_kind not in {"secondary", "model_output"}:
                    raise ValueError("System instructions are not independent world evidence")
                if source["parent_source_refs"] and item.source_kind not in {"secondary", "model_output"}:
                    raise ValueError("Derived sources retain their non-primary lineage")
                for ref in item.verification_refs:
                    self._source(con, claim.scope, ref, path)
                evidence_id = uid("knowledge_evidence", identity.principal, claim_id, req.idempotency_key, index)
                snapshot = EvidenceSnapshot(ref=RecordRef(id=evidence_id, version=1), source_ref=item.source_ref,
                    scope=claim.scope, target=draft.target(), relation=item.relation, content_check=item.content_check,
                    source_kind=item.source_kind, origin_ids=tuple(source["origin_ids"]), origin_status=item.origin_status,
                    source_actor_id=source["actor_id"], source_digest=source["source_digest"],
                    excerpt=source["content"][item.span_start:item.span_end], verification_refs=item.verification_refs,
                    provenance=self._provenance(identity, item.review_reason, "content_match", [item.source_ref.id]))
                evidence_records.append(self.db.put(con, identity=evidence_id, principal=identity.principal,
                    kind="knowledge_evidence", conversation=claim.scope.conversation_id,
                    data={"snapshot": snapshot.model_dump(), "span_start": item.span_start, "span_end": item.span_end,
                          "review_mode": "manual_attestation"}, reason="knowledge_evidence_reviewed"))
            data = draft.model_dump()
            data["provenance"] = self._provenance(identity, req.reason, "evidence_set_change",
                [claim.id, *[item["id"] for item in evidence_records]]).model_dump()
            updated = self._put_claim(con, record, data, "knowledge_evidence_set_replaced")
            return self._ack(updated, *evidence_records)
        return self._command(identity, "evidence.replace", req, operation, claim_id)

    def _evidence(self, con, claim, path=None):
        result = []
        for record in self.db.list(con, claim.scope.principal_id, "knowledge_evidence", claim.scope.conversation_id):
            if record["data"]["snapshot"]["target"] != claim.target().model_dump():
                continue
            data = record["data"]
            item = EvidenceSnapshot.model_validate(data["snapshot"])
            if item.scope != claim.scope or item.ref != self._ref(record):
                raise Conflict("Canonical evidence identity/scope mismatch")
            source = self._source(con, claim.scope, item.source_ref, path)["data"]
            if (item.source_digest != source["source_digest"] or item.source_actor_id != source["actor_id"]
                    or list(item.origin_ids) != source["origin_ids"]
                    or item.excerpt != source["content"][data["span_start"]:data["span_end"]]):
                raise Conflict("Evidence differs from its canonical source")
            for ref in item.verification_refs:
                self._source(con, claim.scope, ref, path)
            result.append(item)
        return tuple(sorted(result, key=lambda item: item.ref.id))

    def _result_ref(self, con, claim, ref, path):
        record = self._resolve(con, claim.scope.principal_id, ref, None)
        if record["kind"] == "knowledge_source":
            self._source(con, claim.scope, ref, path)
        elif record["kind"] == "knowledge_evidence":
            item = EvidenceSnapshot.model_validate(record["data"]["snapshot"])
            if item.target != claim.target() or item.scope != claim.scope:
                raise Conflict("Search result evidence is for another target")
            self._source(con, claim.scope, item.source_ref, path)
        else:
            raise ValueError("Search results must reference canonical knowledge sources/evidence")
        return record

    def record_search(self, identity, claim_id, value):
        self._require(identity, "review")
        req = SearchRecord.model_validate(value)
        def operation(con):
            _, claim, path = self._claim(con, identity, claim_id, req)
            self._claim_origins(con, claim, path)
            for ref in req.result_refs:
                self._result_ref(con, claim, ref, path)
            identity_id = uid("knowledge_search", identity.principal, claim_id, req.idempotency_key)
            stamp = now()
            snapshot = CounterevidenceSearch(ref=RecordRef(id=identity_id, version=1), target=claim.target(),
                scope=claim.scope, status=req.status, search_scope=req.search_scope,
                planned_queries=req.planned_queries, executed_queries=req.executed_queries,
                result_refs=req.result_refs, executor_version=req.executor_version, coverage=req.coverage,
                limitations=req.limitations, stop_reason=req.stop_reason,
                completed_at=stamp if req.status == "completed" else None,
                provenance=self._provenance(identity, req.reason, "search_execution", [r.id for r in req.result_refs], stamp))
            record = self.db.put(con, identity=identity_id, principal=identity.principal, kind="knowledge_search",
                conversation=claim.scope.conversation_id,
                data={"snapshot": snapshot.model_dump(), "execution_mode": "manual_attestation"},
                reason="knowledge_search_attested")
            return self._ack(record)
        return self._command(identity, "search.record", req, operation, claim_id)

    def review_claim(self, identity, claim_id, value):
        self._require(identity, "review")
        req = ClaimReview.model_validate(value)
        def operation(con):
            record, claim, path = self._claim(con, identity, claim_id, req)
            # Non-adopt reviews may retire an off-path claim; they still require
            # authentic in-scope references and the current path revision.
            source_path = path if req.action == "adopt" else None
            self._claim_origins(con, claim, source_path)
            evidence = self._evidence(con, claim, source_path)
            searches = []
            for ref in req.search_refs:
                stored = self._resolve(con, identity.principal, ref, "knowledge_search")
                search = CounterevidenceSearch.model_validate(stored["data"]["snapshot"])
                if search.ref != ref or search.target != claim.target() or search.scope != claim.scope:
                    raise Conflict("Search attestation target is stale or outside scope")
                for result in search.result_refs:
                    self._result_ref(con, claim, result, source_path)
                searches.append(search)
            stamp = now()
            review_id = uid("knowledge_review", identity.principal, claim_id, req.idempotency_key)
            state = {"adopt": "established", "hold": "provisional", "dispute": "disputed",
                     "propose_withdrawal": "withdrawal_candidate", "withdraw": "withdrawal_candidate"}[req.action]
            review = KnowledgeReview(ref=RecordRef(id=review_id, version=1), target=claim.target(), scope=claim.scope,
                action=req.action, state_after=state, profile=req.profile, assessment=req.assessment,
                evidence=evidence, counterevidence=CounterevidenceReview(status=req.counterevidence_status,
                    searches=tuple(searches), evidence_refs=req.counterevidence_refs, resolution=req.counterevidence_resolution,
                    provenance=self._provenance(identity, req.reason, "counterevidence_review", [r.id for r in req.search_refs], stamp)),
                source_independence=SourceIndependence(status=req.source_independence, pairs=req.independence_pairs,
                    provenance=self._provenance(identity, req.reason, "source_independence", [e.ref.id for e in evidence], stamp)),
                review_after=req.review_after, reopens_withdrawal_ref=req.reopens_withdrawal_ref,
                provenance=self._provenance(identity, req.reason, "knowledge_review", [claim.id], stamp))
            data = claim.model_dump()
            data.update(knowledge_state=state, assessment=review.assessment.model_dump(),
                counterevidence_status=review.counterevidence.status, source_independence=review.source_independence.status,
                latest_review_ref=review.ref.model_dump(), review_after=review.review_after,
                provenance=self._provenance(identity, req.reason, "knowledge_state", [claim.id, review_id], stamp).model_dump())
            withdrawal_time = None
            if claim.withdrawal_review_ref is not None:
                withdrawal = self._resolve(con, identity.principal, claim.withdrawal_review_ref, "knowledge_review")
                withdrawn = KnowledgeReview.model_validate(withdrawal["data"]["snapshot"])
                if withdrawn.action != "withdraw" or withdrawn.target.claim_id != claim.id or withdrawn.scope != claim.scope:
                    raise Conflict("Invalid canonical withdrawal ledger")
                withdrawal_time = withdrawn.provenance.created_at
            if req.action == "adopt":
                pending = self.service.countersearch.adoption_readiness(
                    con, identity, claim, req.path_revision, req.search_refs)
                if pending:
                    raise ValueError("Knowledge countersearch prevents adoption: " + ", ".join(pending))
                decision = review_decision(ClaimSnapshot.model_validate(data), review, ReviewContext(scope=claim.scope,
                    evaluated_at=stamp, review_authorized=True, path_eligible=True, references_resolvable=True,
                    withdrawal_ref=claim.withdrawal_review_ref, withdrawal_created_at=withdrawal_time))
                if not decision.eligible:
                    raise ValueError("Knowledge review cannot adopt: " + ", ".join(decision.reasons))
            inspection = self.service.knowledge_observations.review_gate(con, claim, evidence,
                req.independence_pairs, enforce=req.action == "adopt", path_revision=req.path_revision)
            if req.action == "withdraw":
                data["withdrawal_review_ref"] = review.ref.model_dump()
            stored_review = self.db.put(con, identity=review_id, principal=identity.principal, kind="knowledge_review",
                conversation=claim.scope.conversation_id,
                data={"snapshot": review.model_dump(), "review_mode": "manual_attestation",
                      "expected_record_version": req.expected_version, "committed_record_version": req.expected_version + 1,
                      "path_revision": req.path_revision, "query_applicability": "not_evaluated",
                      "inspection": inspection},
                reason="knowledge_review_recorded")
            updated = self._put_claim(con, record, data, "knowledge_" + req.action)
            return self._ack(updated, stored_review)
        return self._command(identity, "claim.review", req, operation, claim_id)

    def get_claim(self, identity, claim_id):
        self._require(identity, "read")
        with self.db.connection() as con:
            return historical_view(self.db.get(con, identity.principal, claim_id, "knowledge_claim"))

    def history(self, identity, claim_id):
        self._require(identity, "read")
        with self.db.connection() as con:
            current = self.db.get(con, identity.principal, claim_id, "knowledge_claim")
            history = [r for r in self.db.list(con, identity.principal, "knowledge_claim_version", current["conversation_id"])
                       if r["data"]["claim_id"] == claim_id]
            return {"current": historical_view(current), "versions": [historical_view(r) for r in history],
                    "current_applicability": "not_evaluated"}

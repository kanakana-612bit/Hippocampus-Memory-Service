"""K3 explicit content/dependency inspection; never a truth-score calculator.

Search execution, inspecting its results, and adopting a claim are separate
commands. Prior findings remain review obligations across evidence replacement.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Assessment, now, uid
from .knowledge_commands import ClaimCommand
from .knowledge_contracts import (CounterevidenceSearch, EvidenceSnapshot,
    HistoricalRecord, IndependencePair, KnowledgeContract, KnowledgeScope,
    ClaimTarget, RecordRef, Text)
from .knowledge_service import historical_view
from .countersearch_contracts import SearchAttemptSnapshot
from .storage import Conflict


OBSERVATION_POLICY = "knowledge.observation.k3.v1"


class ResultInspection(KnowledgeContract):
    result_ref: RecordRef
    disposition: Literal["irrelevant", "evidence_linked", "unresolved"]
    reason: Text
    evidence_refs: tuple[RecordRef, ...] = ()
    source_reliability: Assessment = Field(default_factory=lambda: Assessment(target="source_reliability"))

    @model_validator(mode="after")
    def separate_targets(self):
        if self.source_reliability.target != "source_reliability":
            raise ValueError("Source reliability cannot set world-claim confidence")
        if (self.disposition == "evidence_linked") != bool(self.evidence_refs):
            raise ValueError("Only evidence_linked requires canonical evidence references")
        if len({ref.id for ref in self.evidence_refs}) != len(self.evidence_refs):
            raise ValueError("Repeated evidence reference")
        return self


class DependencyInspection(KnowledgeContract):
    pair: IndependencePair
    verification_refs: tuple[RecordRef, ...] = ()
    method: Literal["original_provenance_inspection", "shared_lineage", "unresolved"]

    @model_validator(mode="after")
    def substantiated_independence(self):
        if self.pair.status == "independent" and (
                self.method != "original_provenance_inspection" or not self.verification_refs):
            raise ValueError("Independence needs inspection of original provenance and references")
        if self.method == "unresolved" and self.pair.status != "unknown":
            raise ValueError("Unresolved provenance cannot certify dependence or independence")
        if len({ref.id for ref in self.verification_refs}) != len(self.verification_refs):
            raise ValueError("Repeated verification reference")
        return self


class ObservationCreate(ClaimCommand):
    search_ref: RecordRef | None = None
    attempt_ref: RecordRef | None = None
    results: tuple[ResultInspection, ...] = ()
    dependencies: tuple[DependencyInspection, ...] = ()

    @model_validator(mode="after")
    def nonempty_unique(self):
        if not self.results and not self.dependencies:
            raise ValueError("An observation must inspect results or source dependencies")
        if self.search_ref is not None and self.attempt_ref is not None:
            raise ValueError("Inspect a search or an interrupted attempt in one command")
        if len({item.result_ref.id for item in self.results}) != len(self.results):
            raise ValueError("Repeated result inspection")
        pairs = [frozenset(item.pair.evidence_ids) for item in self.dependencies]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Repeated dependency pair")
        return self


class ObservationSnapshot(HistoricalRecord):
    ref: RecordRef
    scope: KnowledgeScope
    target: ClaimTarget
    search_ref: RecordRef | None
    attempt_ref: RecordRef | None = None
    results: tuple[ResultInspection, ...]
    dependencies: tuple[DependencyInspection, ...]
    path_revision: Text
    evaluation_mode: Literal["authorized_manual_inspection"] = "authorized_manual_inspection"
    policy_version: Literal["knowledge.observation.k3.v1"] = OBSERVATION_POLICY


class KnowledgeObservationService:
    def __init__(self, service):
        self.service, self.db, self.knowledge = service, service.db, service.knowledge

    def _result_source(self, con, claim, ref):
        """Historical findings can be inspected off-path, but never become support there."""
        record = self.knowledge._resolve(con, claim.scope.principal_id, ref, None)
        if record["kind"] == "knowledge_source":
            return self.knowledge._source(con, claim.scope, ref)
        if record["kind"] != "knowledge_evidence":
            raise ValueError("An inspection result must be a source or evidence record")
        evidence = EvidenceSnapshot.model_validate(record["data"]["snapshot"])
        if evidence.scope != claim.scope or evidence.target.claim_id != claim.id:
            raise Conflict("Evidence finding is outside this claim")
        return self.knowledge._source(con, claim.scope, evidence.source_ref)

    def record(self, identity, claim_id, value):
        k = self.knowledge
        k._require(identity, "review")
        req = ObservationCreate.model_validate(value)

        def operation(con):
            _, claim, path = k._claim(con, identity, claim_id, req)
            # This command can resolve a former branch's concern as irrelevant;
            # current evidence linked by it must still be on the selected path.
            evidence = {item.ref.id: item for item in k._evidence(con, claim, path)}
            if req.search_ref:
                record = k._resolve(con, identity.principal, req.search_ref, "knowledge_search")
                search = CounterevidenceSearch.model_validate(record["data"]["snapshot"])
                if search.scope != claim.scope or search.target.claim_id != claim.id:
                    raise Conflict("Search observation crosses claim or scope")
                if {ref for ref in search.result_refs} != {item.result_ref for item in req.results}:
                    raise ValueError("Inspect every result of the selected search exactly once")
            if req.attempt_ref:
                record = k._resolve(con, identity.principal, req.attempt_ref, "knowledge_search_attempt")
                attempt = SearchAttemptSnapshot.model_validate(record["data"])
                if attempt.scope != claim.scope or attempt.target.claim_id != claim.id:
                    raise Conflict("Attempt observation crosses claim or scope")
                if set(attempt.result_refs) != {item.result_ref for item in req.results}:
                    raise ValueError("Inspect every result of the selected attempt exactly once")
            for item in req.results:
                source = self._result_source(con, claim, item.result_ref)
                if req.search_ref is None and req.attempt_ref is None:
                    result = k._resolve(con, identity.principal, item.result_ref, "knowledge_evidence")
                    if result["data"]["snapshot"]["relation"] not in {"refutes", "qualifies"}:
                        raise ValueError("Standalone result inspection is for historical counterevidence")
                for ref in item.evidence_refs:
                    linked = evidence.get(ref.id)
                    if linked is None or linked.ref != ref or linked.source_ref != k._ref(source):
                        raise ValueError("Linked evidence must belong to the current target and the result's source")
                for ref_id in item.source_reliability.evidence_refs:
                    basis = self.db.get(con, identity.principal, ref_id)
                    self._result_source(con, claim, k._ref(basis))
            for item in req.dependencies:
                if not set(item.pair.evidence_ids) <= evidence.keys():
                    raise ValueError("Dependency inspection requires current canonical evidence")
                left, right = (evidence[key] for key in item.pair.evidence_ids)
                if item.pair.status == "independent" and (left.origin_status != "known"
                        or right.origin_status != "known" or set(left.origin_ids) & set(right.origin_ids)
                        or left.source_ref == right.source_ref):
                    raise ValueError("Shared or unknown origins cannot become independent")
                for ref in item.verification_refs:
                    source = k._source(con, claim.scope, ref, path)
                    if source["data"]["role"] in {"assistant", "system"}:
                        raise ValueError("Model output/instructions cannot attest original provenance")
                    # A user/tool wrapper must not discard an already known
                    # model origin. _source validated these immutable event refs.
                    roots = [self.db.get(con, identity.principal, origin, "event")
                             for origin in source["data"]["origin_ids"]]
                    if any(root["data"]["role"] in {"assistant", "system"} for root in roots):
                        raise ValueError("Known model origins cannot attest original provenance")
            record_id = uid("knowledge_search_observation", identity.principal, claim_id, req.idempotency_key)
            stamp = now()
            provenance = k._provenance(identity, req.reason, "search_result_inspection",
                [claim_id, *([req.search_ref.id] if req.search_ref else []),
                 *([req.attempt_ref.id] if req.attempt_ref else []), *[item.result_ref.id for item in req.results]], stamp)
            provenance.policy_version = OBSERVATION_POLICY
            snapshot = ObservationSnapshot(ref=RecordRef(id=record_id, version=1), scope=claim.scope,
                target=claim.target(), search_ref=req.search_ref, attempt_ref=req.attempt_ref,
                results=req.results, dependencies=req.dependencies,
                path_revision=req.path_revision, provenance=provenance)
            stored = self.db.put(con, identity=record_id, principal=identity.principal,
                kind="knowledge_search_observation", conversation=claim.scope.conversation_id,
                data={"snapshot": snapshot.model_dump()}, reason="knowledge_search_results_inspected")
            return k._ack(stored)

        return k._command(identity, "search.observe", req, operation, claim_id)

    def review_gate(self, con, claim, evidence, declared_pairs, *, enforce, path_revision):
        """Server-resolved obligations; caller cannot hide an inconvenient search.

        All historical results/counterevidence for this claim carry forward. Only
        fresh inspection for the current target/path can discharge an obligation.
        Review of an old result does not make an old search eligible for adoption.
        """
        k = self.knowledge
        obligations = {}
        for record in self.db.list(con, claim.scope.principal_id, "knowledge_search", claim.scope.conversation_id):
            search = CounterevidenceSearch.model_validate(record["data"]["snapshot"])
            if search.scope == claim.scope and search.target.claim_id == claim.id:
                for ref in search.result_refs:
                    obligations[ref] = self._result_source(con, claim, ref)
        for record in self.db.list(con, claim.scope.principal_id, "knowledge_search_attempt", claim.scope.conversation_id):
            attempt = SearchAttemptSnapshot.model_validate(record["data"])
            if attempt.scope == claim.scope and attempt.target.claim_id == claim.id:
                for ref in attempt.result_refs:
                    obligations[ref] = self._result_source(con, claim, ref)
        current = {item.ref: item for item in evidence}
        for record in self.db.list(con, claim.scope.principal_id, "knowledge_evidence", claim.scope.conversation_id):
            item = EvidenceSnapshot.model_validate(record["data"]["snapshot"])
            if item.scope == claim.scope and item.target.claim_id == claim.id and item.ref not in current \
                    and item.relation in {"refutes", "qualifies"} and item.content_check != "mismatched":
                obligations[item.ref] = self._result_source(con, claim, item.ref)

        # Timestamps may tie. The append-only audit sequence is the commit order.
        records = [self.db.decode(row) for row in con.execute(
            "SELECT r.* FROM records r JOIN audit_events a ON a.event_id=r.latest_audit_event_id "
            "WHERE r.principal_id=? AND r.conversation_id=? AND r.kind='knowledge_search_observation' "
            "ORDER BY a.sequence", (claim.scope.principal_id, claim.scope.conversation_id))]
        results, dependencies, used, dependent_pairs = {}, {}, {}, set()
        dependency_sources = {}
        for record in records:
            item = ObservationSnapshot.model_validate(record["data"]["snapshot"])
            if item.scope != claim.scope or item.target.claim_id != claim.id:
                continue
            current_inspection = item.target == claim.target() and item.path_revision == path_revision
            if current_inspection:
                for result in item.results:
                    results[result.result_ref] = result
            for dependency in item.dependencies:
                sources = []
                for evidence_id in dependency.pair.evidence_ids:
                    stored = self.db.get(con, claim.scope.principal_id, evidence_id, "knowledge_evidence")
                    prior = EvidenceSnapshot.model_validate(stored["data"]["snapshot"])
                    source = k._source(con, claim.scope, prior.source_ref)
                    dependency_sources[prior.source_ref] = source
                    sources.append(prior.source_ref)
                keys = frozenset(sources)
                dependencies[keys] = (dependency, current_inspection)
                if dependency.pair.status == "dependent":
                    dependent_pairs.add(keys)
                elif dependency.pair.status == "independent":
                    # Only a new, substantiated original-provenance inspection
                    # can challenge a former dependence. Unknown cannot erase it.
                    dependent_pairs.discard(keys)
            used[item.ref] = item

        problems = []
        for ref, source in obligations.items():
            inspection = results.get(ref)
            if inspection is None or inspection.disposition == "unresolved":
                problems.append("search_result_unresolved:" + ref.id)
                continue
            source_current = [item for item in evidence if item.source_ref == k._ref(source)]
            if inspection.disposition == "irrelevant":
                if any(item.relation in {"refutes", "qualifies"} and item.content_check != "mismatched"
                       for item in source_current):
                    problems.append("irrelevant_result_has_counterevidence:" + ref.id)
            else:
                if not inspection.evidence_refs or any(link not in current or current[link].content_check != "matched"
                        or current[link].source_ref != k._ref(source) for link in inspection.evidence_refs):
                    problems.append("result_evidence_not_resolved:" + ref.id)

        evidence_by_id = {item.ref.id: item for item in evidence}
        # Known shared origins and explicitly reviewed hidden dependencies form
        # connected components. Different record/URL IDs cannot break this chain.
        for item in evidence:
            dependency_sources[item.source_ref] = k._source(con, claim.scope, item.source_ref)
        parent = {key: key for key in dependency_sources}

        def root(key):
            while parent[key] != key:
                key = parent[key]
            return key

        def union(left, right):
            parent[root(left)] = root(right)

        ids = list(dependency_sources)
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                a, b = dependency_sources[left]["data"], dependency_sources[right]["data"]
                if set(a["origin_ids"]) & set(b["origin_ids"]):
                    union(left, right)
        for keys in dependent_pairs:
            if len(keys) == 2:
                union(*keys)
        for pair in declared_pairs:
            if not set(pair.evidence_ids) <= evidence_by_id.keys():
                problems.append("dependency_evidence_missing")
                continue
            sources = [evidence_by_id[key].source_ref for key in pair.evidence_ids]
            keys = frozenset(sources)
            finding, is_current = dependencies.get(keys, (None, False))
            if pair.status == "independent":
                if finding is None or finding.pair.status != "independent" or not is_current:
                    problems.append("independence_inspection_missing")
                if root(sources[0]) == root(sources[1]):
                    problems.append("independence_dependency_conflict")
            elif finding and pair.status != finding.pair.status:
                problems.append("dependency_inspection_conflict")
        if enforce and problems:
            raise ValueError("Knowledge inspection prevents adoption: " + ", ".join(problems))
        return {"policy_version": OBSERVATION_POLICY,
                "observation_refs": [ref.model_dump() for ref in used],
                "obligation_refs": [ref.model_dump() for ref in obligations],
                "unresolved": problems, "assessment_semantics": "authorized_inspection_not_truth_proof"}

    def get(self, identity, observation_id):
        self.knowledge._require(identity, "read")
        with self.db.connection() as con:
            return historical_view(self.db.get(con, identity.principal, observation_id, "knowledge_search_observation"))

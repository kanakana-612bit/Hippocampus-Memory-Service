"""K3 actual bounded corpus scans with expiring leases and immutable attempt history.

Only lexical source retrieval is automated. Claims, evidence judgments, confidence,
weights, independence and adoption are never updated by this service.
"""
from __future__ import annotations

from datetime import timedelta
import time
import uuid

from .contracts import Provenance, canonical, digest, now, timestamp, uid
from .countersearch_contracts import (
    COUNTERSEARCH_POLICY, EXECUTOR_VERSION, CorpusSource, SearchAttemptSnapshot,
    SearchExecute, SearchPlanCreate, SearchPlanSnapshot,
)
from .knowledge_contracts import ClaimSnapshot, CounterevidenceSearch, RecordRef
from .knowledge_service import historical_view
from .storage import Conflict, Missing


LIMITATIONS = (
    "Only the canonical sources frozen in this plan were searched; no external search ran.",
    "Casefolded substring/OR-term matching does not decide support, refutation, or source independence.",
    "A completed search with zero hits is not a finding that counterevidence does not exist.",
    "The time budget is checked between source operations, not a hard preemptive deadline.",
)


class _LeaseExpired(ValueError):
    pass


class CountersearchService:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.knowledge = service.knowledge
        self.monotonic = time.monotonic

    @staticmethod
    def _provenance(identity, reason, target, causes=(), *, stamp=None, expiry=None):
        return Provenance(source="authorized_host:" + identity.host, reason=reason,
            confidence=None, confidence_target=target, created_at=stamp or now(), expiry=expiry,
            policy_version=COUNTERSEARCH_POLICY, cause_refs=list(causes))

    def create_plan(self, identity, claim_id, value):
        self.knowledge._require(identity, "review")
        req = SearchPlanCreate.model_validate(value)

        def operation(con):
            record, claim, path = self.knowledge._claim(con, identity, claim_id, req)
            self.knowledge._claim_origins(con, claim, path)
            corpus, excluded = [], 0
            for source in self.db.list(con, identity.principal, "knowledge_source", claim.scope.conversation_id):
                if source["data"]["scope"] != claim.scope.model_dump():
                    excluded += 1
                    continue
                ref = self.knowledge._ref(source)
                if any(item["id"] not in path["data"]["event_ids"] for item in source["data"]["event_refs"]):
                    excluded += 1
                    continue
                self.knowledge._source(con, claim.scope, ref, path)
                corpus.append(CorpusSource(ref=ref, source_digest=source["data"]["source_digest"]))
            plan_id = uid("knowledge_search_plan", identity.principal, claim_id, req.idempotency_key)
            plan = SearchPlanSnapshot(ref=RecordRef(id=plan_id, version=1), claim_ref=self.knowledge._ref(record),
                target=claim.target(), scope=claim.scope, path_revision=req.path_revision,
                queries=req.queries, not_applicable_reasons=req.not_applicable_reasons,
                budgets=req.budgets, lease_seconds=req.lease_seconds,
                corpus=tuple(sorted(corpus, key=lambda item: item.ref.id)), excluded_source_count=excluded,
                provenance=self._provenance(identity, req.reason, "countersearch_plan", [claim_id]))
            stored = self.db.put(con, identity=plan_id, principal=identity.principal,
                kind="knowledge_search_plan", conversation=claim.scope.conversation_id,
                data=plan.model_dump(), reason="countersearch_plan_created")
            return self.knowledge._ack(stored)

        return self.knowledge._command(identity, "countersearch.plan", req, operation, claim_id)

    def _plan(self, con, identity, plan_id):
        record = self.db.get(con, identity.principal, plan_id, "knowledge_search_plan")
        plan = SearchPlanSnapshot.model_validate(record["data"])
        if plan.ref != self.knowledge._ref(record) or plan.scope.principal_id != identity.principal:
            raise Conflict("Canonical search plan identity mismatch")
        return plan

    def _current(self, con, identity, plan, revision):
        if revision != plan.path_revision:
            raise Conflict("Search plan is bound to another selected path revision")
        path = self.knowledge._path(con, plan.scope, revision)
        record = self.db.get(con, identity.principal, plan.target.claim_id, "knowledge_claim")
        claim = ClaimSnapshot.model_validate(record["data"])
        if (claim.target() != plan.target or claim.scope != plan.scope or claim.record_version != record["version"]
                or record["version"] != plan.claim_ref.version):
            raise Conflict("Search plan claim or evidence-set revision is stale")
        self.knowledge._claim_origins(con, claim, path)
        return path

    def _acquire(self, identity, plan_id, req):
        payload = {"actor_host_id": identity.host, "plan_id": plan_id, **req.model_dump()}

        def operation(con):
            plan = self._plan(con, identity, plan_id)
            self._current(con, identity, plan, req.path_revision)
            lease_id = uid("knowledge_search_lease", identity.principal, plan_id)
            try:
                old = self.db.get(con, identity.principal, lease_id, "knowledge_search_lease")
            except Missing:
                old = None
            stamp = now()
            if old and old["data"]["state"] == "active" and timestamp(old["data"]["expiry"]) > timestamp(stamp):
                raise Conflict("Another execution holds the active search lease")
            attempt_id = uid("knowledge_search_attempt", identity.principal, plan_id, req.idempotency_key)
            expiry = (timestamp(stamp) + timedelta(seconds=plan.lease_seconds)).isoformat()
            data = {"plan_ref": plan.ref.model_dump(), "scope": plan.scope.model_dump(), "attempt_id": attempt_id,
                "host_id": identity.host, "token": uuid.uuid4().hex, "state": "active", "created_at": stamp,
                "phase": "acquired", "expiry": expiry, "path_revision": req.path_revision,
                "provenance": self._provenance(identity, req.reason, "countersearch_lease", [plan_id],
                    stamp=stamp, expiry=expiry).model_dump()}
            lease = self.db.put(con, identity=lease_id, principal=identity.principal, kind="knowledge_search_lease",
                conversation=plan.scope.conversation_id, data=data, reason="countersearch_lease_acquired",
                expected=old["version"] if old else None)
            return {"lease_ref": self.knowledge._ref(lease).model_dump(), "attempt_id": attempt_id, "started_at": stamp}

        return self.db.command(identity.principal, "knowledge.countersearch.acquire", req.idempotency_key, payload, operation)

    def _begin_execution(self, identity, plan_id, req, acquisition, token):
        # Each invocation gets a different server nonce. A racing duplicate cannot
        # reuse the begin receipt to run the executor a second time.
        payload = {"actor_host_id": identity.host, "plan_id": plan_id, "acquisition": acquisition,
                   "invocation_id": uuid.uuid4().hex}

        def operation(con):
            lease = self.db.get(con, identity.principal, acquisition["lease_ref"]["id"], "knowledge_search_lease")
            data = lease["data"]
            if (lease["version"] != acquisition["lease_ref"]["version"] or data["phase"] != "acquired"
                    or data["state"] != "active" or data["host_id"] != identity.host or data["token"] != token):
                raise Conflict("Search execution was already claimed or replaced")
            stamp = now()
            if timestamp(stamp) >= timestamp(data["expiry"]):
                raise _LeaseExpired("Search lease expired before execution could begin")
            updated = {**data, "phase": "running", "invocation_id": payload["invocation_id"],
                "provenance": self._provenance(identity, "countersearch_execution_claimed", "countersearch_lease",
                    [plan_id, acquisition["attempt_id"]], stamp=stamp, expiry=data["expiry"]).model_dump()}
            record = self.db.put(con, identity=lease["id"], principal=identity.principal,
                kind="knowledge_search_lease", conversation=lease["conversation_id"], data=updated,
                reason="countersearch_execution_claimed", expected=lease["version"])
            return {"lease_version": record["version"]}

        return self.db.command(identity.principal, "knowledge.countersearch.begin", req.idempotency_key, payload, operation)

    def execute(self, identity, plan_id, value):
        self.knowledge._require(identity, "review")
        req = SearchExecute.model_validate(value)
        acquisition = self._acquire(identity, plan_id, req)
        started = self.monotonic()
        with self.db.connection() as con:
            try:
                prior = self.db.get(con, identity.principal, acquisition["attempt_id"], "knowledge_search_attempt")
            except Missing:
                prior = None
            if prior is not None:
                records = [prior]
                if prior["data"]["search_ref"] is not None:
                    records.append(self.knowledge._resolve(con, identity.principal,
                        RecordRef.model_validate(prior["data"]["search_ref"]), "knowledge_search"))
                return self.knowledge._ack(*records)
            plan = self._plan(con, identity, plan_id)
            lease = self.db.get(con, identity.principal, acquisition["lease_ref"]["id"], "knowledge_search_lease")
        result = self._empty_result("blocked", "lease_replaced")
        if (lease["data"]["attempt_id"] == acquisition["attempt_id"] and lease["data"]["state"] == "active"
                and lease["data"].get("phase") == "running"):
            raise Conflict("This search attempt is already executing")
        if (lease["version"] != acquisition["lease_ref"]["version"]
                or lease["data"]["attempt_id"] != acquisition["attempt_id"]
                or lease["data"]["state"] != "active" or lease["data"].get("phase") != "acquired"):
            # A replay never owned this invocation. In particular, it must not
            # close an expired/replaced running attempt before the original worker
            # can durably record its partial observations.
            raise Conflict("This invocation no longer owns the acquired search lease")
        token = lease["data"]["token"]
        started_at = acquisition["started_at"]
        owned_version = acquisition["lease_ref"]["version"]
        expired = timestamp(now()) >= timestamp(lease["data"]["expiry"])
        if token is not None and expired:
            result = self._empty_result("blocked", "lease_expired")
        if token is not None and not expired:
            try:
                begun = self._begin_execution(identity, plan_id, req, acquisition, token)
            except _LeaseExpired:
                return self._finish(identity, plan, req, acquisition, token, owned_version, started_at,
                                    self._empty_result("blocked", "lease_expired"))
            owned_version = begun["lease_version"]
            try:
                with self.db.connection() as con:
                    path = self._current(con, identity, plan, req.path_revision)
                    sources = []
                    for item in plan.corpus[:plan.budgets.source_limit]:
                        if (self.monotonic() - started) * 1000 >= plan.budgets.time_ms:
                            break
                        source = self.knowledge._source(con, plan.scope, item.ref, path)
                        if source["data"]["source_digest"] != item.source_digest or digest(source["data"]["content"]) != item.source_digest:
                            raise Conflict("Frozen corpus source digest mismatch")
                        sources.append((item.ref, source["data"]["content"]))
                # The runner has no open transaction, DB handle, or update authority.
                result = self._run(plan, sources, started)
            except (Conflict, Missing, ValueError):
                result = self._empty_result("blocked", "source_or_target_unavailable")
            except Exception as exc:
                result = self._empty_result("blocked", "executor_error:" + type(exc).__name__)
        return self._finish(identity, plan, req, acquisition, token, owned_version, started_at, result)

    @staticmethod
    def _empty_result(status, reason):
        return {"status": status, "stop_reason": reason, "result_refs": [], "executed_query_indices": [],
                "query_results": [], "coverage": {"scanned_pairs": 0, "elapsed_ms": 0}}

    def _run(self, plan, sources, started):
        result = self._empty_result("completed", None)
        found = {}
        budget = plan.budgets

        def stop(reason):
            result.update(status="deferred", stop_reason=reason)

        for index, query in enumerate(plan.queries):
            if index >= budget.query_limit:
                stop("query_budget_exhausted")
                break
            if query.expected_information_gain < budget.min_expected_information_gain:
                stop("low_expected_information_gain")
                break
            if (self.monotonic() - started) * 1000 >= budget.time_ms:
                stop("time_budget_exhausted")
                break
            result["executed_query_indices"].append(index)
            query_result = {"query_index": index, "purpose": query.purpose, "scanned_sources": 0,
                            "matched_source_refs": [], "coverage_complete": False}
            result["query_results"].append(query_result)
            needles = tuple(term.casefold() for term in query.terms) or (query.text.casefold(),)
            for ref, text in sources:
                if (self.monotonic() - started) * 1000 >= budget.time_ms:
                    stop("time_budget_exhausted")
                    break
                query_result["scanned_sources"] += 1
                result["coverage"]["scanned_pairs"] += 1
                if any(term in text.casefold() for term in needles):
                    if ref.id not in found and len(found) >= budget.result_limit:
                        stop("result_budget_exhausted")
                        break
                    found[ref.id] = ref.model_dump()
                    query_result["matched_source_refs"].append(ref.model_dump())
            query_result["coverage_complete"] = (
                result["status"] == "completed" and len(sources) == len(plan.corpus)
                and query_result["scanned_sources"] == len(plan.corpus))
            if result["status"] != "completed":
                break
        if result["status"] == "completed" and len(sources) < len(plan.corpus):
            stop("source_budget_exhausted")
        result["result_refs"] = [found[key] for key in sorted(found)]
        result["coverage"].update(planned_queries=len(plan.queries), executed_queries=len(result["executed_query_indices"]),
            frozen_sources=len(plan.corpus), loaded_sources=len(sources), result_count=len(found),
            elapsed_ms=max(0, round((self.monotonic() - started) * 1000, 3)))
        return result

    def _finish(self, identity, plan, req, acquisition, token, owned_version, started_at, result):
        payload = {"actor_host_id": identity.host, "plan_id": plan.ref.id, **req.model_dump(),
                   "acquisition": acquisition}

        def operation(con):
            stamp = now()
            lease = self.db.get(con, identity.principal, acquisition["lease_ref"]["id"], "knowledge_search_lease")
            owns = (token is not None and lease["version"] == owned_version
                and lease["data"]["token"] == token and lease["data"]["host_id"] == identity.host
                and lease["data"]["attempt_id"] == acquisition["attempt_id"] and lease["data"]["state"] == "active")
            valid = owns and timestamp(stamp) < timestamp(lease["data"]["expiry"])
            rejection = None if valid else ("lease_expired" if owns else "lease_replaced")
            if valid:
                try:
                    path = self._current(con, identity, plan, req.path_revision)
                    frozen = {item.ref.id: item for item in plan.corpus}
                    for ref in result["result_refs"]:
                        if ref["id"] not in frozen or frozen[ref["id"]].ref != RecordRef.model_validate(ref):
                            raise Conflict("Search result is outside the frozen corpus")
                        self.knowledge._source(con, plan.scope, RecordRef.model_validate(ref), path)
                except (Conflict, Missing, ValueError):
                    rejection = "path_or_target_changed"
            outcome = dict(result)
            if rejection:
                outcome.update(status="blocked", stop_reason=rejection)
            search_record = None
            if not rejection and outcome["stop_reason"] not in {"source_or_target_unavailable", "lease_replaced"}:
                search_id = uid("knowledge_search", "countersearch", acquisition["attempt_id"])
                snapshot = CounterevidenceSearch(ref=RecordRef(id=search_id, version=1), target=plan.target,
                    scope=plan.scope, status=outcome["status"], search_scope="frozen_plan:" + plan.ref.id,
                    planned_queries=tuple(query.text for query in plan.queries),
                    executed_queries=tuple(plan.queries[index].text for index in outcome["executed_query_indices"]),
                    result_refs=tuple(RecordRef.model_validate(ref) for ref in outcome["result_refs"]),
                    executor_version=EXECUTOR_VERSION, coverage=canonical(outcome["coverage"]), limitations=LIMITATIONS,
                    stop_reason=outcome["stop_reason"], completed_at=stamp if outcome["status"] == "completed" else None,
                    provenance=self._provenance(identity, req.reason, "search_execution", [plan.ref.id], stamp=started_at))
                search_record = self.db.put(con, identity=search_id, principal=identity.principal,
                    kind="knowledge_search", conversation=plan.scope.conversation_id,
                    data={"snapshot": snapshot.model_dump(), "execution_mode": "actual_internal_corpus",
                          "plan_ref": plan.ref.model_dump(), "attempt_id": acquisition["attempt_id"]},
                    reason="countersearch_executed")
            attempt = SearchAttemptSnapshot(ref=RecordRef(id=acquisition["attempt_id"], version=1), plan_ref=plan.ref,
                target=plan.target, scope=plan.scope, path_revision=req.path_revision, host_id=identity.host,
                status=outcome["status"], started_at=started_at, finished_at=stamp, stop_reason=outcome["stop_reason"],
                search_ref=self.knowledge._ref(search_record) if search_record else None,
                result_refs=tuple(RecordRef.model_validate(ref) for ref in outcome["result_refs"]),
                executed_query_indices=tuple(outcome["executed_query_indices"]), query_results=tuple(outcome["query_results"]),
                coverage=outcome["coverage"], limitations=LIMITATIONS,
                provenance=self._provenance(identity, req.reason, "countersearch_attempt", [plan.ref.id], stamp=stamp))
            attempt_record = self.db.put(con, identity=attempt.ref.id, principal=identity.principal,
                kind="knowledge_search_attempt", conversation=plan.scope.conversation_id,
                data=attempt.model_dump(), reason="countersearch_attempt_finished")
            if owns:
                lease_data = dict(lease["data"])
                lease_data.update(state="finished", outcome=outcome["status"],
                    provenance=self._provenance(identity, outcome["stop_reason"] or "search_completed",
                        "countersearch_lease", [attempt.ref.id], stamp=stamp, expiry=lease_data["expiry"]).model_dump())
                self.db.put(con, identity=lease["id"], principal=identity.principal, kind="knowledge_search_lease",
                    conversation=plan.scope.conversation_id, data=lease_data, reason="countersearch_lease_released",
                    expected=lease["version"])
            return self.knowledge._ack(attempt_record, *([search_record] if search_record else []))

        return self.db.command(identity.principal, "knowledge.countersearch.finish", req.idempotency_key, payload, operation)

    def adoption_readiness(self, con, identity, claim, path_revision, search_refs):
        """Return obligations for explicit adoption; this does not assess source meaning.

        A changed record snapshot requires a new plan. Results from old plans remain
        historical evidence obligations, which the review service resolves separately.
        """
        reasons = []
        plans = {}
        for record in self.db.list(con, identity.principal, "knowledge_search_plan", claim.scope.conversation_id):
            plan = SearchPlanSnapshot.model_validate(record["data"])
            if (plan.target == claim.target() and plan.scope == claim.scope and plan.path_revision == path_revision
                    and plan.claim_ref.version == claim.record_version):
                plans[plan.ref.id] = plan
        if not plans:
            reasons.append("current_search_plan_required")
        completed = {}
        for plan_id, plan in plans.items():
            lease_id = uid("knowledge_search_lease", identity.principal, plan_id)
            try:
                lease = self.db.get(con, identity.principal, lease_id, "knowledge_search_lease")
            except Missing:
                reasons.append("countersearch_plan_unexecuted")
                continue
            if lease["data"]["state"] == "active":
                # An expired unfinished lease also leaves an unresolved execution.
                reasons.append("countersearch_execution_unfinished")
                continue
            try:
                record = self.db.get(con, identity.principal, lease["data"]["attempt_id"], "knowledge_search_attempt")
            except Missing:
                reasons.append("countersearch_plan_incomplete")
                continue
            attempt = SearchAttemptSnapshot.model_validate(record["data"])
            if attempt.status != "completed" or attempt.search_ref is None:
                reasons.append("countersearch_plan_incomplete")
                continue
            if (attempt.plan_ref != plan.ref or attempt.scope != claim.scope or attempt.target != claim.target()
                    or lease["data"]["plan_ref"] != plan.ref.model_dump()
                    or lease["data"]["scope"] != claim.scope.model_dump()
                    or attempt.ref != self.knowledge._ref(record)):
                reasons.append("countersearch_attempt_mismatch")
                continue
            search = self.knowledge._resolve(con, identity.principal, attempt.search_ref, "knowledge_search")
            snapshot = CounterevidenceSearch.model_validate(search["data"]["snapshot"])
            if (search["data"].get("execution_mode") != "actual_internal_corpus"
                    or search["data"].get("attempt_id") != attempt.ref.id
                    or search["data"].get("plan_ref") != plan.ref.model_dump()
                    or snapshot.status != "completed" or snapshot.scope != claim.scope
                    or snapshot.target != claim.target() or snapshot.result_refs != attempt.result_refs):
                reasons.append("actual_countersearch_provenance_mismatch")
                continue
            completed[attempt.search_ref.id] = attempt.search_ref
        selected = [RecordRef.model_validate(ref) for ref in search_refs]
        if not any(completed.get(ref.id) == ref for ref in selected):
            reasons.append("actual_countersearch_required")
        return tuple(dict.fromkeys(reasons))

    def get_plan(self, identity, plan_id):
        self.knowledge._require(identity, "read")
        with self.db.connection() as con:
            return historical_view(self.db.get(con, identity.principal, plan_id, "knowledge_search_plan"))

    def get_attempt(self, identity, attempt_id):
        self.knowledge._require(identity, "read")
        with self.db.connection() as con:
            return historical_view(self.db.get(con, identity.principal, attempt_id, "knowledge_search_attempt"))

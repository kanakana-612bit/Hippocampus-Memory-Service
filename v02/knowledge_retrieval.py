"""K4 read-only claim selection and query-use certificates.

Storage remains the sole update source. Lexical relevance, review validity and
query applicability are separate checks; none changes knowledge or confidence.
"""
from __future__ import annotations

from types import SimpleNamespace

from .contracts import Provenance, digest, now, timestamp, uid
from .knowledge_contracts import (
    AdoptionContext, ClaimSnapshot, CounterevidenceSearch, KnowledgeReview, KnowledgeScope, RecordRef,
    adoption_decision,
)
from .knowledge_observations import OBSERVATION_POLICY
from .knowledge_retrieval_contracts import (
    RETRIEVAL_POLICY, ClaimSourceMaterial, ClaimUseSnapshot, ConditionCheck, KnowledgeQueryContext, ReviewExplanation,
)
from .policy import terms
from .storage import Conflict, Missing


ARTIFACT_KINDS = (
    "knowledge_search_plan", "knowledge_search_attempt", "knowledge_search", "knowledge_search_observation",
    "knowledge_evidence", "knowledge_review", "knowledge_search_lease",
)
REVIEW_INVALIDATORS = frozenset({
    "knowledge_search_plan", "knowledge_search_attempt", "knowledge_search", "knowledge_search_observation",
})
STATE_REASONS = {
    "provisional": "provisional_pending_review",
    "disputed": "disputed_requires_resolution",
    "withdrawal_candidate": "withdrawal_candidate_do_not_adopt",
}
STATE_EXPLANATIONS = {
    "provisional": "This claim is provisional and has not been adopted for current use. Its retained report is not established knowledge.",
    "disputed": "This claim is disputed. Preserve the report and review as history, and explain the unresolved dispute rather than asserting the claim as true.",
    "withdrawal_candidate": "This claim is marked for withdrawal and must not be used as current established knowledge. Its original report and reviews remain historical records; explain the recorded withdrawal action and reason separately from that original report.",
    "established": "An adoption review is recorded. Current use still depends on the query-use checks and usage_scope; historical review explanations alone do not authorize adoption.",
}


class KnowledgeRetrievalService:
    def __init__(self, service):
        self.service, self.db, self.knowledge = service, service.db, service.knowledge

    @staticmethod
    def _ref(record):
        return RecordRef(id=record["id"], version=record["version"])

    def _guard(self, con, record):
        row = con.execute("SELECT a.sequence FROM records r JOIN audit_events a ON a.event_id=r.latest_audit_event_id "
                          "WHERE r.id=? AND r.principal_id=?", (record["id"], record["principal_id"])).fetchone()
        if row is None:
            raise Conflict("Knowledge dependency lacks canonical audit provenance")
        return {"ref": self._ref(record).model_dump(), "kind": record["kind"],
                "data_digest": digest(record["data"]), "audit_sequence": row["sequence"]}

    def _artifacts(self, con, claim):
        records, plan_ids = [], set()
        for kind in ARTIFACT_KINDS:
            if kind == "knowledge_search_lease":
                continue
            for record in self.db.list(con, claim.scope.principal_id, kind, claim.scope.conversation_id):
                data = record["data"].get("snapshot", record["data"])
                if data.get("target", {}).get("claim_id") == claim.id and data.get("scope") == claim.scope.model_dump():
                    records.append(record)
                    if kind == "knowledge_search_plan":
                        plan_ids.add(record["id"])
        for record in self.db.list(con, claim.scope.principal_id, "knowledge_search_lease", claim.scope.conversation_id):
            if record["data"].get("plan_ref", {}).get("id") in plan_ids:
                records.append(record)
        return records

    def _source_material(self, con, claim, path, evidence, dependencies):
        sources = {}
        origin_ids = {ref.id for ref in claim.origin_refs}
        refs = {ref.id: ref for ref in claim.origin_refs}
        for item in evidence:
            refs[item.source_ref.id] = item.source_ref
            for ref in item.verification_refs:
                refs[ref.id] = ref
        for ref in refs.values():
            source = self.knowledge._source(con, claim.scope, ref, path)
            dependencies.append(self._guard(con, source))
            body = source["data"]
            for event_ref in body["event_refs"]:
                event = self.knowledge._resolve(con, claim.scope.principal_id, RecordRef.model_validate(event_ref), "event")
                dependencies.append(self._guard(con, event))
            event = self.knowledge._resolve(con, claim.scope.principal_id, RecordRef.model_validate(body["event_ref"]), "event")
            excerpts = []
            for item in evidence:
                if item.source_ref == ref:
                    record = self.knowledge._resolve(con, claim.scope.principal_id, item.ref, "knowledge_evidence")
                    excerpts.append({"evidence_ref": item.ref.model_dump(), "relation": item.relation,
                        "content_check": item.content_check, "excerpt": item.excerpt,
                        "span_start": record["data"]["span_start"], "span_end": record["data"]["span_end"],
                        "provenance": item.provenance.model_dump()})
            sources[ref.id] = ClaimSourceMaterial(source_ref=ref, scope=claim.scope, role=body["role"],
                actor_id=body["actor_id"], source_digest=body["source_digest"],
                model_provenance=event["data"].get("model_provenance", {}),
                parent_source_refs=body["parent_source_refs"], event_refs=body["event_refs"], origin_ids=body["origin_ids"],
                origin_excerpt=body["content"] if ref.id in origin_ids else None, evidence_excerpts=tuple(excerpts))
        return tuple(sources[key] for key in sorted(sources))

    def _claim_version(self, con, record, claim, version, dependencies):
        if version == claim.record_version:
            return claim
        if type(version) is not int or not 1 <= version < claim.record_version:
            raise Conflict("Review claim version is not canonical history")
        stored = self.db.get(con, claim.scope.principal_id,
            uid("knowledge_claim_version", claim.id, version), "knowledge_claim_version")
        body = stored["data"]
        snapshot = ClaimSnapshot.model_validate(body["snapshot"])
        if (stored["conversation_id"] != claim.scope.conversation_id or body["claim_id"] != claim.id
                or body["version"] != version or snapshot.id != claim.id or snapshot.record_version != version
                or snapshot.scope != claim.scope):
            raise Conflict("Historical review claim identity or scope mismatch")
        dependencies.append(self._guard(con, stored))
        return snapshot

    def _review_explanation(self, con, record, claim, ref, roles, path, evaluated_at):
        """Resolve only the review's own canonical history, without adoption logic."""
        dependencies = []
        stored = self.knowledge._resolve(con, claim.scope.principal_id, ref, "knowledge_review")
        body = stored["data"]
        review = KnowledgeReview.model_validate(body["snapshot"])
        expected, committed = body.get("expected_record_version"), body.get("committed_record_version")
        if (stored["conversation_id"] != claim.scope.conversation_id or review.scope != claim.scope
                or review.ref != ref or review.target.claim_id != claim.id
                or body.get("review_mode") != "manual_attestation"
                or type(expected) is not int or type(committed) is not int or expected + 1 != committed
                or not isinstance(body.get("path_revision"), str) or not body["path_revision"]
                or timestamp(review.provenance.created_at) > timestamp(evaluated_at)):
            raise Conflict("Review explanation lacks canonical authorization or identity")
        if "withdrawal_ledger" in roles and review.action != "withdraw":
            raise Conflict("Withdrawal explanation does not describe a withdrawal")
        if "latest_review" in roles and committed != claim.record_version:
            raise Conflict("Latest review explanation is not the current recorded state")
        prior = self._claim_version(con, record, claim, expected, dependencies)
        after = self._claim_version(con, record, claim, committed, dependencies)
        if (prior.target() != review.target or after.target() != review.target
                or prior.proposition != after.proposition or after.latest_review_ref != review.ref
                or after.knowledge_state != review.state_after or after.assessment != review.assessment):
            raise Conflict("Review explanation does not match its committed historical transition")
        self.knowledge._claim_origins(con, prior, path)
        evidence = self.knowledge._evidence(con, prior, path)
        if evidence != review.evidence:
            raise Conflict("Review explanation evidence differs from canonical history")
        # Check all displayed excerpts against the selected path. The historical
        # target is deliberately used here, never replaced by the current target.
        self._source_material(con, prior, path, evidence, dependencies)
        for item in evidence:
            dependencies.append(self._guard(con, self.knowledge._resolve(
                con, claim.scope.principal_id, item.ref, "knowledge_evidence")))
        for search in review.counterevidence.searches:
            canonical = self.knowledge._resolve(con, claim.scope.principal_id, search.ref, "knowledge_search")
            if (canonical["conversation_id"] != claim.scope.conversation_id
                    or CounterevidenceSearch.model_validate(canonical["data"]["snapshot"]) != search):
                raise Conflict("Review explanation search differs from canonical history")
            dependencies.append(self._guard(con, canonical))
            for result in search.result_refs:
                dependencies.append(self._guard(con, self.knowledge._result_ref(con, prior, result, path)))
        dependencies.append(self._guard(con, stored))
        explanation = ReviewExplanation(review_ref=ref, reference_roles=tuple(roles), scope=review.scope,
            target=review.target, reviewed_proposition=prior.proposition,
            expected_claim_record_version=expected, committed_claim_record_version=committed,
            matches_current_target=review.target == claim.target(), recorded_path_revision=body["path_revision"],
            action=review.action, state_after=review.state_after, review_after=review.review_after,
            reopens_withdrawal_ref=review.reopens_withdrawal_ref, reviewed_evidence=evidence,
            search_refs=tuple(search.ref for search in review.counterevidence.searches), provenance=review.provenance)
        return explanation, dependencies

    def _certificate(self, con, claim, record, path, dependencies):
        """Resolve the exact adoption transaction's pre-review N and committed N+1."""
        reasons = []
        if claim.knowledge_state != "established" or claim.latest_review_ref is None:
            reasons = ["not_established", STATE_REASONS.get(claim.knowledge_state, "adoption_review_missing")]
            if claim.withdrawal_review_ref is not None:
                reasons.append("withdrawal_not_reopened")
            return None, (), None, None, reasons
        wrapper = self.knowledge._resolve(con, claim.scope.principal_id, claim.latest_review_ref, "knowledge_review")
        dependencies.append(self._guard(con, wrapper))
        data = wrapper["data"]
        review = KnowledgeReview.model_validate(data["snapshot"])
        expected, committed = data.get("expected_record_version"), data.get("committed_record_version")
        if (type(expected) is not int or type(committed) is not int or expected + 1 != committed
                or committed != record["version"] or committed != claim.record_version):
            return review, (), wrapper, None, ["adoption_commit_mismatch"]
        if (review.action != "adopt" or review.state_after != "established" or review.ref != claim.latest_review_ref
                or review.target != claim.target() or review.scope != claim.scope):
            return review, (), wrapper, None, ["adoption_review_mismatch"]
        inspection = data.get("inspection")
        if (data.get("review_mode") != "manual_attestation" or not isinstance(inspection, dict)
                or inspection.get("policy_version") != OBSERVATION_POLICY or inspection.get("unresolved") != []):
            return review, (), wrapper, None, ["k3_adoption_certificate_required"]
        historical = self.db.get(con, claim.scope.principal_id, uid("knowledge_claim_version", claim.id, expected), "knowledge_claim_version")
        dependencies.append(self._guard(con, historical))
        prior_data = historical["data"]
        prior = ClaimSnapshot.model_validate(prior_data["snapshot"])
        if (prior_data.get("claim_id") != claim.id or prior_data.get("version") != expected
                or prior.record_version != expected or prior.target() != claim.target() or prior.scope != claim.scope
                or prior.proposition != claim.proposition):
            return review, (), wrapper, prior, ["adoption_prior_revision_mismatch"]
        evidence = self.knowledge._evidence(con, claim, path)
        if evidence != review.evidence:
            reasons.append("adoption_evidence_snapshot_mismatch")
        # Authorization is certified by the stored canonical adopt command. A
        # reader does not gain review permission and no review command is called.
        internal = SimpleNamespace(principal=claim.scope.principal_id, host="knowledge-query-resolver")
        reasons.extend(self.service.countersearch.adoption_readiness(con, internal, prior,
            data["path_revision"], [search.ref for search in review.counterevidence.searches]))
        for search in review.counterevidence.searches:
            stored = self.knowledge._resolve(con, claim.scope.principal_id, search.ref, "knowledge_search")
            if CounterevidenceSearch.model_validate(stored["data"]["snapshot"]) != search:
                reasons.append("adoption_search_snapshot_mismatch")
            for ref in search.result_refs:
                result = self.knowledge._result_ref(con, claim, ref, path)
                dependencies.append(self._guard(con, result))
        checked = self.service.knowledge_observations.review_gate(con, prior, evidence,
            review.source_independence.pairs, enforce=False, path_revision=data["path_revision"])
        reasons.extend(checked["unresolved"])
        if checked != inspection:
            reasons.append("adoption_inspection_snapshot_changed")
        return review, evidence, wrapper, prior, reasons

    def _resolve(self, con, record, scope, path, applicable_at, evaluated_at, temporal_scope):
        claim = ClaimSnapshot.model_validate(record["data"])
        dependencies = [self._guard(con, record)]
        artifacts = self._artifacts(con, claim)
        artifact_guards = [self._guard(con, item) for item in artifacts]
        dependencies.extend(artifact_guards)
        reasons, review, evidence, wrapper = [], None, (), None
        source_material = ()
        explanations = []
        if claim.scope != scope or claim.record_version != record["version"]:
            reasons.append("canonical_scope_or_version_mismatch")
        try:
            self.knowledge._claim_origins(con, claim, path)
            review, evidence, wrapper, _, failures = self._certificate(con, claim, record, path, dependencies)
            reasons.extend(failures)
            if wrapper is not None:
                sequence = self._guard(con, wrapper)["audit_sequence"]
                if any(item["kind"] in REVIEW_INVALIDATORS and item["audit_sequence"] > sequence for item in artifact_guards):
                    reasons.append("new_information_requires_review")
            source_material = self._source_material(con, claim, path, evidence, dependencies)
        except (Missing, Conflict, ValueError, KeyError, TypeError):
            reasons.append("canonical_references_unavailable")
        references = {}
        for role, ref in (("latest_review", claim.latest_review_ref), ("withdrawal_ledger", claim.withdrawal_review_ref)):
            if ref is not None:
                references.setdefault(ref, []).append(role)
        for ref, roles in references.items():
            try:
                explanation, guards = self._review_explanation(con, record, claim, ref, roles, path, evaluated_at)
                explanations.append(explanation)
                dependencies.extend(guards)
            except (Missing, Conflict, ValueError, KeyError, TypeError):
                reasons.append("review_explanation_unavailable")
        checks = tuple(ConditionCheck(condition=value, status="unknown", source="condition_resolver.k4.v1",
            reason="No authorized typed condition resolver is configured; text similarity does not establish applicability")
            for value in claim.proposition.conditions)
        if checks:
            reasons.append("conditions_unknown")
        effective_at = applicable_at
        if effective_at is None and temporal_scope == "current":
            effective_at = evaluated_at
        if effective_at is None and (claim.proposition.valid_from is not None or claim.proposition.valid_until is not None):
            reasons.append("applicability_time_unresolved")
        if review is not None and wrapper is not None:
            withdrawal_time = None
            try:
                if claim.withdrawal_review_ref is not None:
                    withdrawal = self.knowledge._resolve(con, scope.principal_id, claim.withdrawal_review_ref, "knowledge_review")
                    withdrawn = KnowledgeReview.model_validate(withdrawal["data"]["snapshot"])
                    dependencies.append(self._guard(con, withdrawal))
                    if withdrawn.action != "withdraw" or withdrawn.target.claim_id != claim.id or withdrawn.scope != scope:
                        raise Conflict("Canonical withdrawal identity mismatch")
                    withdrawal_time = withdrawn.provenance.created_at
                decision = adoption_decision(claim, review, AdoptionContext(scope=scope, evaluated_at=evaluated_at,
                    applicable_at=effective_at or evaluated_at, conditions_satisfied=not checks,
                    review_authorized=True, path_eligible="canonical_references_unavailable" not in reasons,
                    references_resolvable="canonical_references_unavailable" not in reasons,
                    withdrawal_ref=claim.withdrawal_review_ref, withdrawal_created_at=withdrawal_time))
                reasons.extend(decision.reasons)
            except (Missing, Conflict, ValueError, KeyError, TypeError):
                reasons.append("canonical_adoption_context_unavailable")
        reasons = tuple(dict.fromkeys(reasons))
        eligible = not reasons
        observation_refs = (wrapper["data"].get("inspection", {}).get("observation_refs", []) if wrapper else [])
        snapshot = ClaimUseSnapshot(claim_ref=self._ref(record), target=claim.target(), scope=scope,
            proposition=claim.proposition, knowledge_state=claim.knowledge_state, assessment=claim.assessment,
            counterevidence_status=claim.counterevidence_status, source_independence=claim.source_independence,
            review_ref=claim.latest_review_ref, review_after=claim.review_after,
            evidence_refs=tuple(item.ref for item in evidence), search_refs=tuple(item.ref for item in review.counterevidence.searches) if review else (),
            observation_refs=observation_refs, source_material=source_material, condition_checks=checks,
            state_explanation=STATE_EXPLANATIONS[claim.knowledge_state] + (
                " The recorded withdrawal has not been explicitly reopened; a hold, edit, or retained historical report does not cancel it."
                if "withdrawal_not_reopened" in reasons else "") + (
                " Referenced review details are unavailable in the current authorized scope and path; do not infer the missing review reason."
                if "review_explanation_unavailable" in reasons else ""), review_explanations=tuple(explanations),
            usage_scope="adopted_in_declared_scope" if eligible else "unadopted_do_not_assert_as_truth",
            reasons=reasons, applicable_at=effective_at, evaluated_at=evaluated_at,
            provenance=Provenance(source="knowledge_query_resolver", reason="query_use_eligible" if eligible else ";".join(reasons),
                confidence=None, confidence_target="query_applicability", created_at=evaluated_at,
                expiry=claim.review_after, policy_version=RETRIEVAL_POLICY, cause_refs=[claim.id, *([claim.latest_review_ref.id] if claim.latest_review_ref else [])]))
        unique = {(item["ref"]["id"], item["ref"]["version"]): item for item in dependencies}
        guards = [unique[key] for key in sorted(unique)]
        return snapshot, guards

    def select(self, con, principal, conversation_id, path_revision, query, context_key="conversation",
               sandbox_id=None, applicable_at=None, evaluated_at=None, temporal_scope="current"):
        query_context = KnowledgeQueryContext(applicable_at=applicable_at)
        if temporal_scope not in {"current", "historical", "future", "all"}:
            raise ValueError("Unknown knowledge temporal scope")
        stamp = evaluated_at or now()
        timestamp(stamp)
        scope = KnowledgeScope(principal_id=principal, conversation_id=conversation_id, context_key=context_key,
            domain="fiction" if sandbox_id else "world", sandbox_id=sandbox_id)
        path = self.service.check_path(con, principal, conversation_id, path_revision)
        tokens = sorted(terms(query))
        selected, adopted, caveats = [], [], []
        for record in self.db.list(con, principal, "knowledge_claim", conversation_id):
            if record["data"].get("scope") != scope.model_dump():
                continue
            try:
                self.knowledge._claim_origins(con, ClaimSnapshot.model_validate(record["data"]), path)
            except (Missing, Conflict, ValueError, KeyError, TypeError):
                # Off-path claim text is not leaked through a caveat compartment.
                continue
            text = record["data"]["proposition"]["text"].casefold()
            score = sum(term in text for term in tokens) / max(1, len(tokens))
            if not score:
                continue
            snapshot, dependencies = self._resolve(con, record, scope, path, query_context.applicable_at, stamp, temporal_scope)
            section = "dictionary_claims" if snapshot.usage_scope == "adopted_in_declared_scope" else "knowledge_caveats"
            selected.append({"claim_ref": snapshot.claim_ref.model_dump(), "target": snapshot.target.model_dump(),
                "score": score, "section": section, "reasons": list(snapshot.reasons),
                "review_ref": snapshot.review_ref.model_dump() if snapshot.review_ref else None,
                "evidence_refs": [ref.model_dump() for ref in snapshot.evidence_refs],
                "condition_checks": [check.model_dump() for check in snapshot.condition_checks],
                "dependencies": dependencies})
            (adopted if section == "dictionary_claims" else caveats).append(snapshot.model_dump(mode="json"))
        selected.sort(key=lambda item: (-item["score"], item["claim_ref"]["id"]))
        rank = {item["claim_ref"]["id"]: index for index, item in enumerate(selected)}
        adopted.sort(key=lambda item: rank[item["claim_ref"]["id"]])
        caveats.sort(key=lambda item: rank[item["claim_ref"]["id"]])
        manifest = {"policy_version": RETRIEVAL_POLICY, "scope": scope.model_dump(), "path_revision": path_revision,
            "query_terms": tokens, "requested_applicable_at": applicable_at, "temporal_scope": temporal_scope,
            "selected": selected, "compiled_claim_refs": [item["claim_ref"] for item in adopted],
            "caveat_claim_refs": [item["claim_ref"] for item in caveats]}
        return {"selected": selected, "dictionary_claims": adopted, "knowledge_caveats": caveats,
                "manifest": manifest, "freshness_digest": digest(manifest)}

    def revalidate_manifest(self, con, principal, manifest):
        try:
            if manifest.get("policy_version") != RETRIEVAL_POLICY or manifest["scope"]["principal_id"] != principal:
                return False
            scope = KnowledgeScope.model_validate(manifest["scope"])
            fresh = self.select(con, principal, scope.conversation_id, manifest["path_revision"],
                " ".join(manifest["query_terms"]), context_key=scope.context_key, sandbox_id=scope.sandbox_id,
                applicable_at=manifest["requested_applicable_at"], temporal_scope=manifest["temporal_scope"])
            return fresh["freshness_digest"] == digest(manifest)
        except (Missing, Conflict, ValueError, KeyError, TypeError):
            return False

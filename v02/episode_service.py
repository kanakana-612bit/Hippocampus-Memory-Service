"""Append-only derived episodes, grounded proposals and scoped multi-resolution recall.

Model work happens outside this module/DB transactions. All generated content is
an inferred secondary record, never a new source, independent corroboration, or
automatic proof of a user's position. Only authenticated review changes adoption.
"""
from __future__ import annotations

from datetime import timedelta, timezone
import json

from .contracts import digest, now, provenance, timestamp, uid
from .episode_contracts import (ArtifactReferenceCreate, ChunkSeal, EpisodeProposal,
    EpisodeQuery, JobCommit, JobPrepare, POLICY_VERSION, SourceSpan, StructuredReview,
    raw_text_digest)
from .knowledge_contracts import RecordRef
from .policy import terms
from .storage import Conflict, Missing


JST = timezone(timedelta(hours=9))
LEVEL = {"local": 1, "multi_day": 2, "theme": 3}
DERIVED = {"episode_chunk", "episode_summary", "episode", "structured_candidate", "artifact_reference"}


def ref(record):
    return {"id": record["id"], "version": record["version"]}


class EpisodeService:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def _path(self, con, principal, scope, revision):
        return self.service.check_path(con, principal, scope.conversation_id, revision)

    def _events(self, con, principal, scope, path):
        return [r for identity in path["data"]["event_ids"]
                if (r := self.db.get(con, principal, identity, "event"))["data"].get("sandbox_id") == scope.sandbox_id
                and r["data"].get("context_key", "conversation") == scope.context_key]

    def _resolve(self, con, principal, item, scope, path, kind=None):
        item = item.model_dump() if hasattr(item, "model_dump") else item
        record = self.db.get(con, principal, item["id"], kind)
        if record["version"] != item["version"]:
            raise Conflict("Stale episode source version")
        if record["conversation_id"] != scope.conversation_id:
            raise ValueError("Episode sources cannot cross conversation scope")
        data = record["data"]
        if record["kind"] == "event":
            if (data.get("sandbox_id") != scope.sandbox_id or
                    data.get("context_key", "conversation") != scope.context_key or
                    record["id"] not in path["data"]["event_ids"]):
                raise Conflict("Raw source is outside the selected path/scope")
        else:
            if data.get("scope") != scope.model_dump():
                raise ValueError("Derived source scope mismatch")
            if not set(data.get("source_event_ids", [])) <= set(path["data"]["event_ids"]):
                raise Conflict("Derived source is outside the current branch")
            for source in data.get("source_event_ids", []):
                event = self.db.get(con, principal, source, "event")
                if event["conversation_id"] != scope.conversation_id or event["data"].get("sandbox_id") != scope.sandbox_id or event["data"].get("context_key", "conversation") != scope.context_key:
                    raise ValueError("Derived source origin scope mismatch")
        return record

    def _spans(self, con, principal, spans, scope, path, allowed=None, user_only=False):
        origins = set()
        for value in spans:
            span = SourceSpan.model_validate(value)
            record = self._resolve(con, principal, span.event_ref, scope, path, "event")
            text = record["data"]["content"]
            if (raw_text_digest(text) != span.source_digest or span.end > len(text) or
                    text[span.start:span.end] != span.quote):
                raise ValueError("Source span does not match immutable original text")
            if allowed is not None and record["id"] not in allowed:
                raise ValueError("Proposal cites a source not supplied in its job")
            if user_only and record["data"]["role"] != "user":
                raise ValueError("A user-specific structured memory requires original user speech")
            origins.add(record["id"])
        return origins

    def _eligible(self, con, principal, kind, scope, path):
        result = []
        for record in self.db.list(con, principal, kind, scope.conversation_id):
            try:
                self._resolve(con, principal, ref(record), scope, path)
            except (Missing, Conflict, ValueError):
                continue
            result.append(record)
        return result

    @staticmethod
    def _heads(records):
        obsolete = {r["data"]["supersedes_ref"]["id"] for r in records if r["data"].get("supersedes_ref")}
        return [r for r in records if r["id"] not in obsolete]

    def _current_derivations(self, con, principal, scope, path):
        records = self._eligible(con, principal, "episode_summary", scope, path) + self._eligible(con, principal, "episode", scope, path)
        active = {r["id"]: r for r in self._heads(records)}
        # A retained ancestor is history if an immutable child was regenerated.
        # Never substitute the new child into the old ancestor's historical text.
        while True:
            stale = {identity for identity, r in active.items()
                     if any(c["id"] not in active for c in r["data"].get("child_refs", []))}
            if not stale:
                break
            active = {key: value for key, value in active.items() if key not in stale}
        return list(active.values())

    def _pending_response_users(self, con, principal, conversation, events):
        users = [e["id"] for e in events if e["data"]["role"] == "user"]
        if not users:
            return set()
        pending = set()
        for attempt in self.db.list(con, principal, "attempt", conversation):
            data = attempt["data"]
            if data.get("response_event_id") or data.get("dispatch_status") in {"completed", "failed"} or data.get("host_status") == "blocked_capacity":
                continue
            run = self.db.get(con, principal, data["run_id"], "run")
            run_users = [i for i in run["data"]["input_event_ids"] if i in users]
            if run_users:
                pending.add(run_users[-1])
        return pending

    def seal(self, principal, value):
        from .episode_chunking import plan_chunks
        req = ChunkSeal.model_validate(value)
        def operation(con):
            path = self._path(con, principal, req.scope, req.path_revision)
            events = self._events(con, principal, req.scope, path)
            existing = self._eligible(con, principal, "episode_chunk", req.scope, path)
            sealed_set = {i for r in existing for i in r["data"]["source_event_ids"]}
            sealed = [e["id"] for e in events if e["id"] in sealed_set]
            pending_users = self._pending_response_users(con, principal, req.scope.conversation_id,
                [e for e in events if e["id"] not in sealed_set])
            pending = req.response_in_progress or bool(pending_users)
            plan = plan_chunks(events, [e["id"] for e in events], now=now(),
                already_sealed_event_ids=sealed, response_in_progress=pending, policy=req.policy)
            created = []
            newly_sealed = set()
            for chunk in plan.sealed:
                if pending_users & set(chunk.event_ids):
                    break  # Keep a contiguous prefix; even an older in-flight response belongs to its original chunk.
                data = chunk.model_dump(mode="json")
                identity = uid("episode_chunk", principal, req.scope.model_dump(), list(chunk.event_ids))
                saved = self.db.put(con, identity=identity, principal=principal, kind="episode_chunk",
                    conversation=req.scope.conversation_id, reason="idle_chunk_sealed", data={
                        **data, "scope": req.scope.model_dump(), "path_revision": req.path_revision,
                        "source_event_ids": list(chunk.event_ids), "policy": req.policy.model_dump(),
                        "processing_boundary_only": True, "sealed_at": now(),
                        "provenance": provenance("idle_chunker", req.reason, target="processing_boundary",
                            causes=chunk.event_ids, policy=POLICY_VERSION)})
                created.append(ref(saved))
                newly_sealed.update(chunk.event_ids)
            return {"chunk_refs": created, "pending_event_ids": [e["id"] for e in events if e["id"] not in sealed_set | newly_sealed],
                    "response_in_progress": pending, "warnings": list(plan.warnings), "raw_changed": False}
        # The deterministic chunk identity and write transaction make concurrent
        # polls idempotent. Empty polls never append canonical receipt noise.
        with self.db.connection(write=True) as con:
            return operation(con)

    def _job_inputs(self, con, principal, req, path):
        summaries = self._heads(self._eligible(con, principal, "episode_summary", req.scope, path))
        if req.operation == "provisional":
            done = {r["data"]["chunk_ref"]["id"] for r in summaries}
            return [r for r in self._eligible(con, principal, "episode_chunk", req.scope, path) if r["id"] not in done]
        if req.operation == "regenerate":
            inputs = [self._resolve(con, principal, r, req.scope, path) for r in req.source_refs]
            if any(r["kind"] not in DERIVED for r in inputs):
                raise ValueError("Regeneration requires a chunk, summary, episode, candidate or artifact reference")
            return inputs
        day = req.day or (timestamp(now()).astimezone(JST).date() - timedelta(days=1)).isoformat()
        if day >= timestamp(now()).astimezone(JST).date().isoformat():
            raise ValueError("Daily organization processes completed local days only")
        processed = set()
        for state in self.db.list(con, principal, "episode_job_state", req.scope.conversation_id):
            data = state["data"]
            if data.get("scope") == req.scope.model_dump() and data.get("operation") == "daily" and data.get("status") == "committed":
                processed.update(r["id"] for r in data.get("input_refs", []))
        result = []
        for summary in summaries:
            chunk = self._resolve(con, principal, summary["data"]["chunk_ref"], req.scope, path, "episode_chunk")
            if timestamp(chunk["data"]["ended_at"]).astimezone(JST).date().isoformat() <= day and summary["id"] not in processed:
                result.append(summary)
        return result

    def _bundle(self, con, principal, job, path):
        from .contracts import Scope
        data = job["data"]
        scope = Scope.model_validate(data["scope"])
        records = [self._resolve(con, principal, item, scope, path) for item in data["input_refs"]]
        episodes = [self._resolve(con, principal, item, scope, path, "episode") for item in data["existing_episode_refs"]]
        origins = set(i for record in records + episodes for i in record["data"].get("source_event_ids", []))
        sources = []
        for identity in path["data"]["event_ids"]:
            if identity not in origins:
                continue
            record = self.db.get(con, principal, identity, "event")
            d = record["data"]
            sources.append({"event_ref": ref(record), "source_digest": raw_text_digest(d["content"]),
                "raw_text": d["content"], "speaker": d["role"], "event_time": d["event_time"],
                "room": d.get("room_id"), "context_key": d.get("context_key", "conversation"),
                "conversation_id": record["conversation_id"], "session_id": d.get("session_id"),
                "provenance": d["provenance"]})
        chunks = []
        seen = set()
        for record in records:
            candidate = record if record["kind"] == "episode_chunk" else self._resolve(con, principal,
                record["data"]["chunk_ref"], scope, path, "episode_chunk") if record["kind"] == "episode_summary" else None
            if candidate and candidate["id"] not in seen:
                seen.add(candidate["id"])
                chunks.append({"ref": ref(candidate), **candidate["data"]})
        return {"sources": sources, "chunks": chunks,
            "summaries": [{"ref": ref(r), **r["data"]} for r in records if r["kind"] == "episode_summary"],
            "regeneration_inputs": [{"ref": ref(r), "kind": r["kind"], **r["data"]} for r in records] if data["operation"] == "regenerate" else [],
            "existing_episodes": [{"ref": ref(r), **r["data"]} for r in episodes],
            "scope": data["scope"], "path_revision": data["path_revision"], "day": data.get("day"),
            "processing_boundary_is_not_episode_boundary": True}

    def prepare(self, principal, host, value):
        req = JobPrepare.model_validate(value)
        # No-work polls do not create receipts or records. The immutable manifest
        # ID supplies idempotency across concurrent workers and restarts.
        with self.db.connection(write=True) as con:
            path = self._path(con, principal, req.scope, req.path_revision)
            inputs = self._job_inputs(con, principal, req, path)
            if not inputs:
                return {"status": "no_work", "job_id": None, "operation": req.operation}
            episodes = [r for r in self._current_derivations(con, principal, req.scope, path) if r["kind"] == "episode"]
            data = {"scope": req.scope.model_dump(), "path_revision": req.path_revision, "host_id": host,
                "operation": req.operation, "day": req.day, "input_refs": [ref(r) for r in inputs],
                "existing_episode_refs": [ref(r) for r in episodes], "policy_version": POLICY_VERSION}
            if req.operation == "regenerate":
                data["regeneration_request"] = req.idempotency_key
            manifest = digest(data)
            identity = uid("episode_job", principal, manifest)
            try:
                job = self.db.get(con, principal, identity, "episode_job")
            except Missing:
                job = self.db.put(con, identity=identity, principal=principal, kind="episode_job",
                    conversation=req.scope.conversation_id, reason="episode_job_prepared", data={**data,
                    "manifest_digest": manifest, "provenance": provenance(host, req.reason,
                        target="derived_memory_job", causes=[r["id"] for r in inputs], policy=POLICY_VERSION)})
            state = None
            try:
                state = self.db.get(con, principal, uid("episode_job_state", identity), "episode_job_state")
            except Missing:
                pass
            if state and state["data"]["status"] == "committed":
                return {"status": "no_work", "job_id": None, "operation": req.operation}
            return {"status": state["data"]["status"] if state else "prepared", "job_id": identity,
                "manifest_digest": manifest, "operation": req.operation, "path_revision": req.path_revision,
                "input_bundle": self._bundle(con, principal, job, path),
                "proposal_schema": EpisodeProposal.model_json_schema()}

    def commit(self, principal, host, identity, value):
        req = JobCommit.model_validate(value)
        def operation(con):
            from .contracts import Scope
            job = self.db.get(con, principal, identity, "episode_job")
            j = job["data"]
            if j["host_id"] != host or j["manifest_digest"] != req.manifest_digest:
                raise Conflict("Job host or manifest mismatch")
            scope = Scope.model_validate(j["scope"])
            path = self._path(con, principal, scope, j["path_revision"])
            bundle = self._bundle(con, principal, job, path)
            state_id = uid("episode_job_state", identity)
            try:
                prior = self.db.get(con, principal, state_id, "episode_job_state")
            except Missing:
                prior = None
            proposal_digest = digest({"proposal": req.proposal.model_dump(), "generator": req.generator.model_dump()})
            if prior and prior["data"]["status"] == "committed":
                if prior["data"]["proposal_digest"] != proposal_digest:
                    raise Conflict("Job already committed with a different proposal")
                return prior["data"]["result"]
            if j["operation"] in {"daily", "provisional"}:
                pending_request = JobPrepare(scope=scope, path_revision=j["path_revision"], operation=j["operation"],
                    day=j["day"], reason="revalidate_job_inputs", idempotency_key="revalidate")
                pending_ids = {r["id"] for r in self._job_inputs(con, principal, pending_request, path)}
                if not {r["id"] for r in j["input_refs"]} <= pending_ids:
                    raise Conflict("Another job consumed these inputs; prepare again")
            allowed = {s["event_ref"]["id"] for s in bundle["sources"]}
            if not any((req.proposal.summaries, req.proposal.episodes, req.proposal.structured_candidates)):
                raise ValueError("An empty proposal cannot consume pending inputs")
            if j["operation"] == "provisional":
                if {s.chunk_ref.id for s in req.proposal.summaries} != {r["id"] for r in j["input_refs"]} or req.proposal.episodes:
                    raise ValueError("Provisional jobs summarize each input chunk exactly once; semantic episodes belong to daily organization")
            elif j["operation"] == "daily" and (not req.proposal.episodes or req.proposal.summaries):
                raise ValueError("Daily organization requires semantic episodes, not processing chunks")
            produced = {"summary_refs": [], "episode_refs": [], "candidate_refs": []}
            candidates = list(req.proposal.structured_candidates)
            for index, summary in enumerate(req.proposal.summaries):
                chunk = self._resolve(con, principal, summary.chunk_ref, scope, path, "episode_chunk")
                if chunk["id"] not in {c["ref"]["id"] for c in bundle["chunks"]}:
                    raise ValueError("Summary target was not supplied to the job")
                heads = [r for r in self._heads(self._eligible(con, principal, "episode_summary", scope, path))
                         if r["data"]["chunk_ref"]["id"] == chunk["id"]]
                if heads:
                    if j["operation"] != "regenerate" or len(heads) != 1 or summary.supersedes_ref is None or summary.supersedes_ref.model_dump() != ref(heads[0]):
                        raise Conflict("Summary regeneration requires its current predecessor")
                elif summary.supersedes_ref is not None:
                    raise Conflict("Summary predecessor is not current")
                ids = self._spans(con, principal, summary.source_spans, scope, path, set(chunk["data"]["source_event_ids"]))
                if ids != set(chunk["data"]["source_event_ids"]):
                    raise ValueError("Provisional summary must retain provenance to every chunk event")
                for field in ("events", "decision_candidates", "stance_candidates", "intent_candidates", "unresolved_candidates", "related_entities"):
                    for item in getattr(summary, field):
                        self._spans(con, principal, item.source_spans, scope, path, ids,
                                    user_only=field in {"decision_candidates", "stance_candidates", "intent_candidates"})
                record = self._derived(con, principal, scope, "episode_summary", uid("episode_summary", identity, index),
                    summary.model_dump(mode="json"), ids, req, host, identity)
                produced["summary_refs"].append(ref(record))
                from .episode_contracts import StructuredCandidate
                for field, kind in (("decision_candidates", "user_decision"), ("stance_candidates", "user_stance"),
                                    ("intent_candidates", "user_intent"), ("unresolved_candidates", "unresolved")):
                    for item in getattr(summary, field):
                        candidates.append(StructuredCandidate(kind=kind, subject="user" if kind != "unresolved" else "conversation",
                            topic=item.text, text=item.text, source_spans=item.source_spans,
                            episode_refs=(RecordRef(**ref(record)),)))
            created = {}
            remaining = {e.local_id: e for e in req.proposal.episodes}
            while remaining:
                ready = [e for e in remaining.values() if set(e.child_local_ids) <= created.keys()]
                if not ready:
                    raise ValueError("Cyclic episode proposal")
                for episode in ready:
                    children = [self._resolve(con, principal, r, scope, path) for r in episode.child_refs]
                    children.extend(created[i] for i in episode.child_local_ids)
                    if any(r["kind"] not in {"episode", "episode_summary"} for r in children):
                        raise ValueError("Episode children must be summaries or episodes")
                    if any(LEVEL.get(r["data"].get("level"), 0) >= LEVEL[episode.level] for r in children):
                        raise ValueError("Parent episode must have higher abstraction than its children")
                    ids = self._spans(con, principal, episode.source_spans, scope, path, allowed)
                    ids.update(i for r in children for i in r["data"]["source_event_ids"])
                    if not ids or not ids <= allowed:
                        raise ValueError("Episode origins must be supplied immutable sources")
                    for relation in episode.relations:
                        self._resolve(con, principal, relation.target_ref, scope, path, "episode")
                    if episode.supersedes_ref:
                        self._resolve(con, principal, episode.supersedes_ref, scope, path, "episode")
                        if any(r["data"].get("supersedes_ref", {}).get("id") == episode.supersedes_ref.id
                               for r in self._eligible(con, principal, "episode", scope, path) if r["data"].get("supersedes_ref")):
                            raise Conflict("Episode predecessor already superseded; rebase regeneration")
                    body = episode.model_dump(mode="json", exclude={"local_id", "child_local_ids"})
                    body["child_refs"] = [ref(r) for r in children]
                    record = self._derived(con, principal, scope, "episode", uid("episode", identity, episode.local_id),
                        body, ids, req, host, identity)
                    produced["episode_refs"].append(ref(record))
                    created[episode.local_id] = record
                    del remaining[episode.local_id]
            if j["operation"] == "daily":
                required = {i for r in j["input_refs"] for i in self.db.get(con, principal, r["id"])["data"]["source_event_ids"]}
                if not required <= {i for e in created.values() for i in e["data"]["source_event_ids"]}:
                    raise ValueError("Daily organization omitted input provenance; split or merge without dropping sources")
            for index, candidate in enumerate(candidates):
                ids = self._spans(con, principal, candidate.source_spans, scope, path, allowed,
                                  user_only=candidate.kind != "unresolved")
                episodes = [self._resolve(con, principal, r, scope, path) for r in candidate.episode_refs]
                episodes.extend(created[i] for i in candidate.episode_local_ids)
                if any(r["kind"] not in {"episode", "episode_summary"} for r in episodes):
                    raise ValueError("Structured memory episode reference has wrong type")
                if episodes and not ids <= {i for e in episodes for i in e["data"]["source_event_ids"]}:
                    raise ValueError("Structured original spans are outside referenced episodes")
                if candidate.supersedes_ref:
                    self._resolve(con, principal, candidate.supersedes_ref, scope, path, "structured_candidate")
                body = candidate.model_dump(mode="json", exclude={"local_id", "episode_local_ids"})
                body["episode_refs"] = [ref(r) for r in episodes]
                body["adoption"] = "candidate"
                record = self._derived(con, principal, scope, "structured_candidate", uid("structured_candidate", identity, index),
                    body, ids, req, host, identity)
                produced["candidate_refs"].append(ref(record))
            result = {"job_id": identity, **produced, "raw_changed": False, "knowledge_promoted": False}
            self.db.put(con, identity=state_id, principal=principal, kind="episode_job_state",
                conversation=scope.conversation_id, expected=prior["version"] if prior else None,
                reason="episode_job_committed", data={"scope": scope.model_dump(), "operation": j["operation"],
                    "status": "committed", "input_refs": j["input_refs"], "proposal_digest": proposal_digest,
                    "result": result, "provenance": provenance(host, req.reason, policy=POLICY_VERSION)})
            return result
        return self.db.command(principal, "episode.job.commit", req.idempotency_key,
            {"job_id": identity, "host": host, **req.model_dump()}, operation)

    def _derived(self, con, principal, scope, kind, identity, body, origins, req, host, job):
        return self.db.put(con, identity=identity, principal=principal, kind=kind, conversation=scope.conversation_id,
            reason="derived_episode_recorded", data={**body, "scope": scope.model_dump(),
                "source_event_ids": sorted(origins), "generator": req.generator.model_dump(), "job_id": job,
                "epistemic_status": "inferred", "assessment": {"target": "report_fidelity", "status": "unknown", "score": None, "calibrated": False},
                "retrieval_weight": 0.5, "salience": 0.5, "generalization_weight": 0.0,
                "usage_scope": "fiction" if scope.sandbox_id else "episode_only",
                "provenance": provenance(host, req.reason, target="derived_memory", causes=sorted(origins), policy=POLICY_VERSION)})

    def _reviews(self, con, principal, scope, path):
        latest = {}
        records = self._eligible(con, principal, "structured_review", scope, path)
        sequence = {r["id"]: r["sequence"] for r in con.execute(
            "SELECT r.id,a.sequence FROM records r JOIN audit_events a ON r.latest_audit_event_id=a.event_id "
            "WHERE r.kind='structured_review' AND r.principal_id=? AND r.conversation_id=?", (principal, scope.conversation_id))}
        for r in sorted(records, key=lambda r: sequence[r["id"]]):
            latest[r["data"]["target_ref"]["id"]] = r
        return latest

    def review(self, principal, host, value):
        req = StructuredReview.model_validate(value)
        def operation(con):
            path = self._path(con, principal, req.scope, req.path_revision)
            target = self._resolve(con, principal, req.target_ref, req.scope, path, "structured_candidate")
            previous = self._reviews(con, principal, req.scope, path).get(target["id"])
            expected = req.expected_review_ref.model_dump() if req.expected_review_ref else None
            if (ref(previous) if previous else None) != expected:
                raise Conflict("Structured review changed; review current history first")
            if req.action == "confirm":
                if previous and previous["data"]["action"] == "supersede":
                    raise Conflict("A superseded statement cannot be reactivated; create a new candidate")
                self._spans(con, principal, target["data"]["source_spans"], req.scope, path,
                    user_only=target["data"]["kind"] != "unresolved")
                for other_id, review in self._reviews(con, principal, req.scope, path).items():
                    if other_id != target["id"] and review["data"]["action"] == "confirm":
                        other = self.db.get(con, principal, other_id, "structured_candidate")
                        if all(other["data"][key] == target["data"][key] for key in ("kind", "subject", "topic")):
                            raise Conflict("A current statement exists for this subject/topic; explicitly supersede it first")
            origins = set(target["data"]["source_event_ids"])
            if req.replacement_ref:
                replacement = self._resolve(con, principal, req.replacement_ref, req.scope, path, "structured_candidate")
                if any(replacement["data"][key] != target["data"][key] for key in ("kind", "subject")):
                    raise ValueError("Replacement must refer to the same structured kind and subject")
                history = self._reviews(con, principal, req.scope, path)
                successor = replacement["id"]
                seen = {target["id"]}
                while successor in history and history[successor]["data"]["action"] == "supersede":
                    if successor in seen:
                        raise ValueError("Structured supersession cycle")
                    seen.add(successor)
                    successor = history[successor]["data"]["replacement_ref"]["id"]
                if successor in seen:
                    raise ValueError("Structured supersession cycle")
                origins.update(replacement["data"]["source_event_ids"])
            if req.unresolved_status is not None and target["data"]["kind"] != "unresolved":
                raise ValueError("Unresolved lifecycle applies only to unresolved items")
            return self.db.put(con, identity=uid("structured_review", principal, req.idempotency_key), principal=principal,
                kind="structured_review", conversation=req.scope.conversation_id, reason="structured_memory_reviewed",
                data={**req.model_dump(mode="json", exclude={"idempotency_key"}), "source_event_ids": sorted(origins),
                    "host_id": host, "confirmation_target": "fidelity_to_explicit_speech_not_world_truth",
                    "provenance": provenance(host, req.reason, target="report_fidelity", causes=sorted(origins), policy=POLICY_VERSION)})
        return self.db.command(principal, "episode.structured.review", req.idempotency_key, {"host": host, **req.model_dump()}, operation)

    def artifact(self, principal, host, value):
        req = ArtifactReferenceCreate.model_validate(value)
        def operation(con):
            path = self._path(con, principal, req.scope, req.path_revision)
            origins = self._spans(con, principal, req.source_spans, req.scope, path)
            if req.supersedes_ref:
                self._resolve(con, principal, req.supersedes_ref, req.scope, path, "artifact_reference")
                if any(r["data"].get("supersedes_ref", {}).get("id") == req.supersedes_ref.id
                       for r in self._eligible(con, principal, "artifact_reference", req.scope, path) if r["data"].get("supersedes_ref")):
                    raise Conflict("Artifact authority already superseded; register against current reference")
            return self.db.put(con, identity=uid("artifact_reference", principal, req.idempotency_key), principal=principal,
                kind="artifact_reference", conversation=req.scope.conversation_id, reason="external_authority_registered",
                data={**req.model_dump(mode="json", exclude={"idempotency_key"}), "source_event_ids": sorted(origins),
                    "current_state_authority": "external", "external_content_fetched": False,
                    "provenance": provenance(host, req.reason, target="external_authority_pointer", causes=sorted(origins), policy=POLICY_VERSION)})
        return self.db.command(principal, "episode.artifact", req.idempotency_key, {"host": host, **req.model_dump()}, operation)

    def query(self, principal, value, con=None):
        req = EpisodeQuery.model_validate(value)
        if con is None:
            with self.db.connection() as connection:
                return self.query(principal, req, connection)
        path = self._path(con, principal, req.scope, req.path_revision)
        tokens = terms(req.query)
        def score(text):
            return len(tokens & terms(text)) / max(1, len(tokens)) if tokens else 1.0
        target = self._resolve(con, principal, req.target_ref, req.scope, path) if req.target_ref else None
        if target and target["kind"] not in DERIVED:
            raise ValueError("Drilldown target must be a derived memory")
        events = self._events(con, principal, req.scope, path)
        if target:
            events = [e for e in events if e["id"] in target["data"]["source_event_ids"]]
        if req.mode == "raw":
            # The FTS is a verified projection of immutable event text. LIKE is
            # additionally necessary for Japanese substrings without whitespace.
            needle = req.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            ids = {r[0] for r in con.execute("SELECT id FROM raw_event_fts WHERE content LIKE ? ESCAPE '\\'", ("%" + needle + "%",))}
            rows = [e for e in events if e["id"] in ids]
            return {"mode": "raw", "sources": [{"event_ref": ref(e), **e["data"]} for e in rows[:req.limit]],
                    "truncated": len(rows) > req.limit, "current_state_verified": False}
        episodes = (self._eligible(con, principal, "episode", req.scope, path) +
                    self._eligible(con, principal, "episode_summary", req.scope, path)) if req.include_history else self._current_derivations(con, principal, req.scope, path)
        if target:
            child_ids = {r["id"] for r in target["data"].get("child_refs", [])}
            episodes = [r for r in episodes if r["id"] in child_ids]
        ranked = [(score(r["data"].get("title", "") + " " + r["data"].get("summary", "")), r) for r in episodes]
        ranked = [(s, r) for s, r in ranked if s > 0 and (req.level is None or r["data"].get("level") == req.level)]
        ranked.sort(key=lambda pair: (-LEVEL.get(pair[1]["data"].get("level"), 0), -pair[0], pair[1]["id"]))
        # A matching ancestor is the entry point; descendants remain available
        # through versioned child refs and explicit drilldown, not duplicated text.
        hidden = set()
        selected = []
        by_id = {r["id"]: r for r in episodes}
        def descendants(r):
            pending = list(r["data"].get("child_refs", []))
            found = set()
            while pending:
                item = pending.pop()
                if item["id"] in found:
                    continue
                found.add(item["id"])
                if item["id"] in by_id:
                    pending.extend(by_id[item["id"]]["data"].get("child_refs", []))
            return found
        for s, record in ranked:
            if record["id"] not in hidden:
                if record["kind"] == "episode_summary" and any(set(record["data"]["source_event_ids"]) <= set(e["source_event_ids"]) for e in selected):
                    continue
                selected.append({"ref": ref(record), "score": s, **record["data"]})
                hidden.update(descendants(record))
        reviews = self._reviews(con, principal, req.scope, path)
        structured = []
        for r in self._eligible(con, principal, "structured_candidate", req.scope, path):
            d = r["data"]
            review = reviews.get(r["id"])
            status = {"confirm": "current", "withdraw": "withdrawn", "supersede": "superseded"}.get(review["data"]["action"] if review else "", "candidate")
            if (status == "current" or req.include_history) and score(d["text"] + " " + d["topic"]) > 0:
                structured.append({"ref": ref(r), **d, "adoption": status, "review_ref": ref(review) if review else None,
                    "unresolved_status": (review["data"].get("unresolved_status") or "active") if review and d["kind"] == "unresolved" else None})
        artifacts = self._eligible(con, principal, "artifact_reference", req.scope, path)
        old_artifacts = {r["data"]["supersedes_ref"]["id"] for r in artifacts if r["data"].get("supersedes_ref")}
        artifacts = [{"ref": ref(r), **r["data"], "requires_authorized_refresh": True} for r in artifacts
            if (req.include_history or r["id"] not in old_artifacts) and score(r["data"]["title"] + " " + r["data"]["uri"]) > 0]
        def reference_view(value):
            # Keep precise source addresses without embedding all original quotes
            # in every abstract summary; /query mode=raw resolves them on demand.
            if isinstance(value, list):
                return [reference_view(v) for v in value]
            if isinstance(value, dict):
                return {k: reference_view(v) for k, v in value.items() if not (k == "quote" and "event_ref" in value)}
            return value
        return {"mode": req.mode, "episodes": reference_view(selected[:req.limit]) if req.mode in {"auto", "episodic"} else [],
            "structured": reference_view(structured[:req.limit]) if req.mode in {"auto", "structured"} else [],
            "artifacts": reference_view(artifacts[:req.limit]) if req.mode in {"auto", "artifact"} else [],
            "truncated": any(len(rows) > req.limit for rows in (selected, structured, artifacts)),
            "priority": ["external_current_state_authority", "confirmed_user_statements", "high_level_episode", "child_episode", "raw"],
            "current_state_verified": False, "raw_preserved": True}

    def manifest(self, con, principal, scope, path):
        # Review and supersession change effective selection without changing
        # historical records; their complete scoped manifest invalidates stale sends.
        return sorted([{"id": r["id"], "version": r["version"], "kind": r["kind"]}
            for kind in ("episode", "episode_summary", "structured_candidate", "structured_review", "artifact_reference")
            for r in self._eligible(con, principal, kind, scope, path)], key=lambda r: r["id"])

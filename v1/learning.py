"""Synthetic-only source ingestion, shared learning and atomic publication."""
from __future__ import annotations

import json
from collections import defaultdict

from .access import combined_policy, effective_identity, enforce, require
from .analysis import PIPELINE_VERSION, PROMPT_VERSION, capacity, validate_proposal
from .common import Conflict, Denied, Invalid, canonical, digest, identifier, now, strict_json
from .contracts import ModelConfig, SourceRange, SourceRegister
from .normalization import POLICY, normalize


class Learning:
    def __init__(self, storage, models, default_model, analyzer):
        self.storage = storage
        self.models = {m.id: m for m in models}
        if default_model not in self.models:
            raise Invalid("Default conversation model must be explicitly configured")
        self.default_model, self.analyzer = default_model, analyzer

    def register_source(self, identity, registration: SourceRegister, raw: bytes, *, key):
        require(identity, "sources.write")
        # Caller must explicitly designate fictional input. Real intake is not exposed.
        if registration.data_class != "synthetic":
            raise Denied("Real intake is outside this implementation phase")
        try:
            document = strict_json(raw.decode("utf-8"))
        except UnicodeError as exc:
            raise Invalid("Raw must be UTF-8 JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list) or not document["messages"]:
            raise Invalid("A nonempty messages array is required")
        source_id = registration.id
        snapshot_id = identifier("snapshot", identity.principal_id, source_id, digest(raw))

        def operation(tx):
            reader, _ = effective_identity(tx.con, identity)
            source = tx.get("raw_sources", id=source_id)
            if source is not None:
                enforce(reader, source)
                if (source["name"] != registration.original_name or source["timezone"] != registration.timezone
                        or source["access_scope"] != registration.access_scope
                        or json.loads(source["destinations_json"]) != list(registration.allowed_destinations)):
                    raise Conflict("Source policy or metadata changes require a separate explicit operation")
                old = tx.get("raw_snapshots", id=source["head_snapshot_id"])
                old_doc = strict_json(old["raw_bytes"].decode("utf-8"))
                old_messages = old_doc.pop("messages")
                metadata = {k: v for k, v in document.items() if k != "messages"}
                if old_doc != metadata or document["messages"][:len(old_messages)] != old_messages:
                    raise Conflict("Source prefix or time metadata changed; register a separate source snapshot lineage")
                if len(document["messages"]) < len(old_messages):
                    raise Conflict("Source append cannot remove messages")
                first_new = len(old_messages) + 1
            else:
                source = tx.insert("raw_sources", {"id": source_id, "principal_id": identity.principal_id,
                    "name": registration.original_name, "access_scope": registration.access_scope,
                    "destinations_json": canonical(registration.allowed_destinations), "timezone": registration.timezone})
                first_new = 1
            if tx.get("raw_snapshots", id=snapshot_id) is None:
                tx.insert("raw_snapshots", {"id": snapshot_id, "source_id": source_id, "principal_id": identity.principal_id,
                    "raw_bytes": raw, "digest": digest(raw), "message_count": len(document["messages"])})
            for sequence in range(first_new, len(document["messages"]) + 1):
                message = document["messages"][sequence - 1]
                if not isinstance(message, dict):
                    raise Invalid("Each raw message must be an object")
                tx.insert("raw_log_messages", {"source_id": source_id, "sequence": sequence, "principal_id": identity.principal_id,
                    "original_json": canonical(message), "digest": digest(message), "status": message.get("status", "completed")})
            if source["head_snapshot_id"] != snapshot_id:
                tx.update("raw_sources", {"id": source_id}, {"head_snapshot_id": snapshot_id})
            if tx.get("learning_checkpoints", source_id=source_id) is None:
                tx.insert("learning_checkpoints", {"source_id": source_id, "principal_id": identity.principal_id})
            return {"source_id": source_id, "snapshot_id": snapshot_id, "message_count": len(document["messages"])}

        return self.storage.command(identity, "source.register", key,
            {"registration": registration.model_dump(), "raw_digest": digest(raw)}, operation,
            guard=lambda tx: enforce(effective_identity(tx.con, identity)[0], tx.get("raw_sources", id=source_id))
                if tx.get("raw_sources", id=source_id) is not None else None)

    def create_run(self, identity, *, mode, ranges=(), source_ids=(), model_id=None, key):
        require(identity, "learning.run")
        if mode not in ("initial", "daily", "reanalysis"):
            raise Invalid("Unknown learning mode")
        config = self.models.get(model_id or self.default_model)
        if config is None:
            raise Invalid("Unknown configured model")
        ranges = [SourceRange.model_validate(r) for r in ranges]
        if mode == "daily" and (ranges or not source_ids):
            raise Invalid("Daily range is fixed from sequence checkpoints, not caller timestamps")
        if mode != "daily" and (source_ids or not ranges):
            raise Invalid("Explicit nonempty write ranges are required")
        requested = {"mode": mode, "ranges": [r.model_dump() for r in ranges],
                     "source_ids": list(source_ids), "model": config.model_dump()}

        def operation(tx):
            reader, _ = effective_identity(tx.con, identity)
            selected = list(ranges)
            if mode == "daily":
                for source_id in source_ids:
                    source = tx.get("raw_sources", id=source_id)
                    enforce(reader, source, destination=config.destination)
                    snapshot = tx.get("raw_snapshots", id=source["head_snapshot_id"])
                    normalized = normalize(snapshot["raw_bytes"], source_id=source_id, epoch=self.storage.epoch, import_timezone=source["timezone"])
                    # U is the last completely captured turn; an unfinished turn blocks all later events.
                    groups = self._turns(normalized["events"])
                    upper = 0
                    for group in groups:
                        if any(e["status"] == "in_progress" for e in group):
                            break
                        upper = group[-1]["sequence"]
                    lower = tx.get("learning_checkpoints", source_id=source_id)["sequence"]
                    if upper > lower:
                        selected.append(SourceRange(source_id=source_id, first=lower + 1, last=upper))
            if len({r.source_id for r in selected}) != len(selected):
                raise Invalid("Use one contiguous write range per source per run")
            run_id = identifier("run", self.storage.epoch, identity.principal_id, key)
            head = tx.get("context_map_heads", principal_id=identity.principal_id)
            base_generation = head["generation_id"] if head else None
            from .memory import Memory
            memory_reader = Memory(self.storage)
            context_reader = reader.model_copy(update={"destination": config.destination})
            visible_records, context_reader, _ = memory_reader._visible(tx.con, context_reader)
            visible_contexts, _ = memory_reader._contexts(tx.con, context_reader, visible_records)
            contexts = list(visible_contexts.values())
            manifest_sources, prepared = [], []
            for selected_range in selected:
                source = tx.get("raw_sources", id=selected_range.source_id)
                enforce(reader, source, destination=config.destination)
                snapshot = tx.get("raw_snapshots", id=source["head_snapshot_id"])
                normalized = normalize(snapshot["raw_bytes"], source_id=source["id"], epoch=self.storage.epoch, import_timezone=source["timezone"])
                events = normalized["events"]
                time_heads = {}
                for event in events:
                    current_time = tx.con.execute("""SELECT h.version,h.revision_id,t.data_json FROM event_time_heads h
                        JOIN event_time_revisions t ON t.id=h.revision_id WHERE h.event_id=? AND h.principal_id=?""", (event["id"], identity.principal_id)).fetchone()
                    if current_time:
                        event["time"] = json.loads(current_time["data_json"])
                        time_heads[event["id"]] = {"revision_id": current_time["revision_id"], "version": current_time["version"]}
                cfirst, clast = selected_range.context_first or selected_range.first, selected_range.context_last or selected_range.last
                if clast > len(events):
                    raise Invalid("Range exceeds pinned snapshot")
                full_turns = self._turns(events)
                starts, ends = {g[0]["sequence"] for g in full_turns}, {g[-1]["sequence"] for g in full_turns}
                if any(first not in starts or last not in ends for first, last in ((selected_range.first, selected_range.last), (cfirst, clast))):
                    raise Invalid("Range must preserve complete user/response/tool turn groups")
                selected_events = events[selected_range.first - 1:selected_range.last]
                if any(e["status"] == "in_progress" for e in selected_events):
                    raise Invalid("Unfinished turn is not a confirmed learning endpoint")
                entry = {"range": selected_range.model_dump(), "snapshot_id": snapshot["id"], "digest": snapshot["digest"],
                    "source_id": source["id"], "original_name": source["name"], "owner": source["principal_id"],
                    "timezone": source["timezone"], "source_version": source["version"], "format": "raw.messages.v1"}
                manifest_sources.append(entry)
                outer_context = [e for e in events[cfirst - 1:clast] if not selected_range.first <= e["sequence"] <= selected_range.last]

                def batch_for(write):
                    readings = sorted(outer_context + write, key=lambda e: e["sequence"])
                    # Context-only spans must already have canonical event referents; no implicit writes.
                    return {"schema_version": "learning.input.v1", "events": readings,
                            "write_event_ids": [e["id"] for e in write], "existing_contexts": contexts,
                            "time_heads": {e["id"]: time_heads[e["id"]] for e in readings if e["id"] in time_heads},
                            "snapshot_id": snapshot["id"], "source_id": source["id"],
                            "warnings": [w for w in normalized["warnings"] if write and write[0]["sequence"] <= w["sequence"] <= write[-1]["sequence"]]}

                accumulated = []
                base_cost = capacity(batch_for([]), config)["input_upper_bound"]
                accumulated_cost = base_cost
                for group in self._turns(selected_events):
                    group_capacity = capacity(batch_for(group), config)
                    if not group_capacity["fits"]:
                        raise Invalid("Single complete turn plus reading context exceeds capacity; explicit larger model/range required")
                    # Additive JSON contributions avoid reserializing an ever-growing
                    # prefix for every turn. Four separators conservatively cover joins.
                    contribution = group_capacity["input_upper_bound"] - base_cost + 4
                    if accumulated and accumulated_cost + contribution + config.output_reserve > config.context_window:
                        prepared.append(batch_for(accumulated))
                        accumulated = []
                        accumulated_cost = base_cost
                    accumulated.extend(group)
                    accumulated_cost += contribution
                if accumulated:
                    prepared.append(batch_for(accumulated))
            manifest = {"schema_version": "learning.manifest.v1", "epoch": self.storage.epoch, "sources": manifest_sources,
                "model": config.model_dump(), "normalization_version": POLICY, "time_policy": "time.v1",
                "pipeline_version": PIPELINE_VERSION, "prompt_version": PROMPT_VERSION, "output_schema": "hippocampus.analysis.v1",
                "context_base_generation": base_generation, "created_at": now(), "data_class": "synthetic"}
            tx.insert("learning_runs", {"id": run_id, "principal_id": identity.principal_id, "mode": mode,
                "manifest_json": canonical(manifest), "status": "pending" if prepared else "complete",
                "release_status": "unpublished" if mode == "initial" and prepared else "published"})
            batch_ids = []
            for index, batch in enumerate(prepared):
                batch_id = identifier("batch", run_id, index)
                written = [e for e in batch["events"] if e["id"] in batch["write_event_ids"]]
                tx.insert("learning_batches", {"id": batch_id, "run_id": run_id, "principal_id": identity.principal_id,
                    "source_id": batch["source_id"], "first_sequence": written[0]["sequence"], "last_sequence": written[-1]["sequence"],
                    "input_json": canonical(batch), "status": "pending"})
                batch_ids.append(batch_id)
            return {"run_id": run_id, "batch_ids": batch_ids, "manifest_digest": digest(manifest), "status": "pending" if prepared else "complete"}

        def guard(tx):
            reader, _ = effective_identity(tx.con, identity)
            for source_id in set(source_ids) | {r.source_id for r in ranges}:
                enforce(reader, tx.get("raw_sources", id=source_id))
                enforce(reader, tx.get("raw_sources", id=source_id), destination=config.destination)
        return self.storage.command(identity, "learning.create", key, requested, operation, guard=guard)

    @staticmethod
    def _turns(events):
        groups = []
        for event in events:
            if not groups or groups[-1][-1]["turn_id"] != event["turn_id"]:
                groups.append([])
            groups[-1].append(event)
        return groups

    def _batch(self, con, identity, batch_id):
        if not con.in_transaction:
            con.execute("BEGIN")
        identity, _ = effective_identity(con, identity)
        batch = con.execute("SELECT * FROM learning_batches WHERE id=? AND principal_id=?", (batch_id, identity.principal_id)).fetchone()
        if batch is None:
            raise Denied("Object is unavailable in this authorization context")
        source = con.execute("SELECT * FROM raw_sources WHERE id=?", (batch["source_id"],)).fetchone()
        run = con.execute("SELECT * FROM learning_runs WHERE id=?", (batch["run_id"],)).fetchone()
        config = ModelConfig.model_validate(json.loads(run["manifest_json"])["model"])
        enforce(identity, source)
        enforce(identity, source, destination=config.destination)
        from .memory import Memory
        memory_reader = Memory(self.storage)
        context_reader = identity.model_copy(update={"destination": config.destination})
        records, context_reader, _ = memory_reader._visible(con, context_reader)
        contexts, _ = memory_reader._contexts(con, context_reader, records)
        for context in json.loads(batch["input_json"])["existing_contexts"]:
            if context["id"] not in contexts or contexts[context["id"]] != context:
                raise Conflict("Context evidence or authorization changed; create a new learning run")
        if batch["status"] != "committed":
            for event_id, dependency in json.loads(batch["input_json"]).get("time_heads", {}).items():
                current = con.execute("SELECT revision_id,version FROM event_time_heads WHERE event_id=?", (event_id,)).fetchone()
                if current is None or dict(current) != dependency:
                    raise Conflict("Time interpretation changed; create a new pinned learning run")
        return batch, run, config

    def analyze(self, identity, batch_id, *, key):
        require(identity, "learning.run")
        with self.storage.connection() as con:
            row, run, config = self._batch(con, identity, batch_id)
            current = con.execute("SELECT * FROM staging WHERE batch_id=?", (batch_id,)).fetchone()
            if current and current["status"] in ("valid", "review_required", "committed"):
                return {"batch_id": batch_id, "status": current["status"], "validation": json.loads(current["validation_json"])}
            batch, expected = json.loads(row["input_json"]), row["version"]
        # Access has been enforced before any content reaches this analyzer.
        value, proposal, error = None, None, None
        try:
            value = self.analyzer.analyze(batch, config)
            proposal = validate_proposal(value, batch)
        except Exception as exc:
            # No provider body or exception text: it may echo protected content or credentials.
            error = type(exc).__name__
        status = "failed" if error else "review_required" if batch["warnings"] else "valid"
        validation = {"validator_version": "evidence.v1", "error_type": error, "warnings": batch["warnings"],
                      "semantic_fidelity": "unassessed", "capacity": capacity(batch, config)}

        def operation(tx):
            current_batch, _, _ = self._batch(tx.con, identity, batch_id)
            if current_batch["version"] != expected:
                raise Conflict("Batch changed during analysis; output was not committed")
            data = {"output_json": canonical(proposal.model_dump()) if proposal else None,
                    "validation_json": canonical(validation), "status": status}
            previous = tx.get("staging", batch_id=batch_id)
            if previous:
                tx.update("staging", {"id": previous["id"]}, data)
            else:
                tx.insert("staging", {"id": identifier("staging", batch_id), "batch_id": batch_id,
                    "principal_id": identity.principal_id, **data})
            tx.update("learning_batches", {"id": batch_id}, {"status": status}, expected=expected)
            return {"batch_id": batch_id, "status": status, "validation": validation}

        return self.storage.command(identity, "learning.analyze", key,
            {"batch_id": batch_id, "expected": expected, "output_digest": digest(proposal.model_dump()) if proposal else None}, operation,
            guard=lambda tx: self._batch(tx.con, identity, batch_id))

    def review(self, identity, batch_id, *, reason, expected_version, key):
        require(identity, "learning.review")
        if not reason.strip():
            raise Invalid("Explicit review reason required")

        def operation(tx):
            self._batch(tx.con, identity, batch_id)
            staged = tx.get("staging", batch_id=batch_id)
            if staged is None or staged["status"] != "review_required":
                raise Conflict("Only time-warning review cases can be acknowledged here")
            validation = json.loads(staged["validation_json"])
            validation["review"] = {"actor": identity.principal_id, "reason": reason, "created_at": now()}
            tx.update("staging", {"id": staged["id"]}, {"status": "valid", "validation_json": canonical(validation)}, expected=expected_version)
            tx.update("learning_batches", {"id": batch_id}, {"status": "valid"})
            return {"batch_id": batch_id, "status": "valid"}

        return self.storage.command(identity, "learning.review", key,
            {"batch_id": batch_id, "reason": reason, "expected": expected_version}, operation,
            guard=lambda tx: self._batch(tx.con, identity, batch_id))

    def commit(self, identity, batch_id, *, key):
        require(identity, "learning.commit")

        def operation(tx):
            reader, _ = effective_identity(tx.con, identity)
            batch_row, run, config = self._batch(tx.con, identity, batch_id)
            staged = tx.get("staging", batch_id=batch_id)
            if staged and staged["status"] == "committed":
                return {"batch_id": batch_id, "status": "committed", "run_id": run["id"]}
            if staged is None or staged["status"] != "valid":
                raise Conflict("Only validated staging can be committed")
            batch = json.loads(batch_row["input_json"])
            proposal = validate_proposal(json.loads(staged["output_json"]), batch)
            source = tx.get("raw_sources", id=batch_row["source_id"])
            principal = identity.principal_id
            manifest = json.loads(run["manifest_json"])
            for context in batch["existing_contexts"]:
                current = tx.get("contexts", id=context["id"])
                enforce(reader, current, destination=config.destination)
                if current["version"] != context["version"]:
                    raise Conflict("Context changed since analysis")
            write_ids = set(batch["write_event_ids"])
            for event in batch["events"]:
                if event["id"] not in write_ids:
                    continue
                data = {k: event[k] for k in ("role", "content", "sequence", "source_id", "turn_id", "status", "source_digest", "raw_role", "tool_call_id", "tool_calls")}
                existing = tx.get("records", id=event["id"])
                if existing:
                    if existing["principal_id"] != principal or existing["kind"] != "event" or json.loads(existing["data_json"]) != data:
                        raise Conflict("Canonical event identity/content conflict")
                    enforce(reader, tx.get("record_access_policies", record_id=event["id"]))
                    if existing["release_id"]:
                        old_release = tx.get("learning_runs", id=existing["release_id"])
                        if old_release["release_status"] != "published" and old_release["id"] != run["id"]:
                            raise Conflict("Event belongs to a different unpublished initial release")
                    # Reanalysis never overwrites a separately corrected time interpretation.
                else:
                    tx.insert("records", {"id": event["id"], "kind": "event", "principal_id": principal,
                        "data_json": canonical(data), "created_at": now(), "release_id": run["id"] if run["mode"] == "initial" else None})
                    tx.insert("record_access_policies", {"record_id": event["id"], "principal_id": principal,
                        "access_scope": source["access_scope"], "destinations_json": source["destinations_json"], "reason": "raw source policy"})
                    tx.insert("event_origins", {"event_id": event["id"], "principal_id": principal, "source_id": source["id"],
                        "sequence": event["sequence"], "snapshot_id": batch["snapshot_id"]})
                    self._initial_time(tx, identity, event)
                    tx.con.execute("INSERT INTO event_fts(id,principal_id,content) VALUES (?,?,?)", (event["id"], principal, event["content"]))

            def evidence_policy(spans):
                policies = []
                for span in spans:
                    event = tx.get("records", id=span.event_id)
                    if event is None or event["kind"] != "event" or event["principal_id"] != principal:
                        raise Conflict("Context-only evidence requires an already canonical event")
                    policy = tx.get("record_access_policies", record_id=span.event_id)
                    enforce(reader, policy, destination=config.destination)
                    policies.append(policy)
                return combined_policy(policies)

            context_ids, memory_ids = {}, {}
            for context in proposal.contexts:
                context_id = identifier("context", batch_id, context.candidate_id)
                scope, destinations = evidence_policy(context.source_spans)
                tx.insert("contexts", {"id": context_id, "principal_id": principal, "name": context.name,
                    "description": context.description, "access_scope": scope, "destinations_json": canonical(destinations)})
                context_ids[context.candidate_id] = context_id
                for index, span in enumerate(context.source_spans):
                    tx.insert("context_source_spans", {"id": identifier("context_span", context_id, index), "principal_id": principal,
                        "context_id": context_id, "event_id": span.event_id, "event_version": span.version, "span_json": canonical(span.model_dump())})
            for memory in proposal.memories:
                memory_id = identifier("memory", batch_id, memory.candidate_id)
                scope, destinations = evidence_policy(memory.source_spans)
                data = memory.model_dump(exclude={"candidate_id", "kind"})
                data.update({"source_event_ids": list(dict.fromkeys(s.event_id for s in memory.source_spans)),
                    "time_dependencies": {s.event_id: tx.get("event_time_heads", event_id=s.event_id)["revision_id"] for s in memory.source_spans},
                    "confidence": None, "epistemic_assessment": {"status": "unassessed", "source": "model_proposal"},
                    "provenance": {"source": batch_id, "reason": "validated model proposal", "confidence": None,
                                   "created_at": now(), "decay": "none; persistent derived memory"}})
                tx.insert("records", {"id": memory_id, "kind": memory.kind, "principal_id": principal,
                    "data_json": canonical(data), "created_at": now(), "release_id": run["id"] if run["mode"] == "initial" else None})
                tx.insert("record_access_policies", {"record_id": memory_id, "principal_id": principal,
                    "access_scope": scope, "destinations_json": canonical(destinations), "reason": "strongest evidence restriction"})
                memory_ids[memory.candidate_id] = memory_id
                for index, span in enumerate(memory.source_spans):
                    tx.insert("record_source_spans", {"id": identifier("record_span", memory_id, index), "principal_id": principal,
                        "record_id": memory_id, "event_id": span.event_id, "event_version": span.version, "span_json": canonical(span.model_dump())})
            # Copy-on-write generation. Preserve manual and all out-of-target assignments.
            head = tx.get("context_map_heads", principal_id=principal)
            active_id = head["generation_id"] if head else None
            draft = tx.get("run_context_maps", run_id=run["id"])
            base_id = draft["generation_id"] if run["mode"] == "initial" and draft else active_id
            generation_id = identifier("map", batch_id)
            tx.insert("context_map_generations", {"id": generation_id, "principal_id": principal, "base_id": base_id,
                "manifest_json": canonical({"run_id": run["id"], "batch_id": batch_id}),
                "status": "draft" if run["mode"] == "initial" else "published"})
            proposed_rows = {}
            for membership in proposal.memberships:
                context_id = membership.context_id or context_ids[membership.context_candidate_id]
                table = "utterance_context_membership" if membership.event_id else "memory_context_membership"
                target_col = "event_id" if membership.event_id else "memory_id"
                target_id = membership.event_id or memory_ids[membership.memory_candidate_id]
                data = {"id": identifier("membership", generation_id, table, target_id, context_id), "principal_id": principal,
                    "generation_id": generation_id, target_col: target_id, "context_id": context_id,
                    "membership_weight": membership.membership_weight, "confidence": membership.confidence,
                    "source": "model_extraction", "evidence_json": canonical([s.model_dump() for s in membership.source_spans]), "assigned_at": now()}
                if target_col == "memory_id":
                    data["target_version"] = 1
                proposed_rows[(table, target_id, context_id)] = data
            for table, target_col in (("utterance_context_membership", "event_id"), ("memory_context_membership", "memory_id")):
                if base_id:
                    for old in tx.con.execute(f"SELECT * FROM {table} WHERE generation_id=?", (base_id,)).fetchall():
                        tuple_key = (table, old[target_col], old["context_id"])
                        if tuple_key in proposed_rows and old["source"] != "manual":
                            continue
                        proposed_rows.pop(tuple_key, None)
                        inherited = dict(old)
                        inherited.update({"id": identifier("membership", generation_id, table, old[target_col], old["context_id"]),
                                          "generation_id": generation_id, "version": 1})
                        tx.insert(table, inherited)
            for (table, _, _), data in proposed_rows.items():
                tx.insert(table, data)
            if draft:
                tx.update("run_context_maps", {"run_id": run["id"]}, {"generation_id": generation_id})
            else:
                tx.insert("run_context_maps", {"run_id": run["id"], "principal_id": principal, "generation_id": generation_id})
            tx.insert("learning_intervals", {"id": identifier("interval", batch_id), "principal_id": principal,
                "source_id": source["id"], "first_sequence": batch_row["first_sequence"], "last_sequence": batch_row["last_sequence"], "batch_id": batch_id})
            tx.update("staging", {"id": staged["id"]}, {"status": "committed"})
            tx.update("learning_batches", {"id": batch_id}, {"status": "committed"})
            pending = tx.con.execute("SELECT 1 FROM learning_batches WHERE run_id=? AND status!='committed'", (run["id"],)).fetchone()
            publish = run["mode"] != "initial" or not pending
            if publish:
                if run["mode"] == "initial" and active_id != manifest["context_base_generation"]:
                    raise Conflict("Published context map changed during initial learning; explicit new analysis required")
                tx.update("context_map_generations", {"id": generation_id}, {"status": "published"})
                if head:
                    tx.update("context_map_heads", {"principal_id": principal}, {"generation_id": generation_id})
                else:
                    tx.insert("context_map_heads", {"principal_id": principal, "generation_id": generation_id})
            tx.update("learning_runs", {"id": run["id"]}, {"status": "pending" if pending else "complete",
                "release_status": "published" if publish else "unpublished"})
            if publish:
                for source_id in {entry["source_id"] for entry in manifest["sources"]}:
                    self._advance_checkpoint(tx, principal, source_id)
            return {"batch_id": batch_id, "status": "committed", "run_id": run["id"]}

        return self.storage.command(identity, "learning.commit", key, {"batch_id": batch_id}, operation,
            guard=lambda tx: self._batch(tx.con, identity, batch_id))

    @staticmethod
    def _initial_time(tx, identity, event):
        revision_id = identifier("time", event["id"], 1)
        tx.insert("event_time_revisions", {"id": revision_id, "event_id": event["id"], "principal_id": identity.principal_id,
            "data_json": canonical(event["time"]), "reason": "shared normalization policy", "actor": identity.principal_id, "created_at": now()})
        tx.insert("event_time_heads", {"event_id": event["id"], "principal_id": identity.principal_id, "revision_id": revision_id})

    @staticmethod
    def _advance_checkpoint(tx, principal, source_id):
        checkpoint = tx.get("learning_checkpoints", source_id=source_id)
        position = checkpoint["sequence"]
        intervals = tx.con.execute("""SELECT i.first_sequence,i.last_sequence FROM learning_intervals i
            JOIN learning_batches b ON b.id=i.batch_id JOIN learning_runs r ON r.id=b.run_id
            WHERE i.source_id=? AND i.principal_id=? AND r.release_status='published' ORDER BY i.first_sequence""", (source_id, principal))
        for interval in intervals:
            if interval["first_sequence"] <= position + 1:
                position = max(position, interval["last_sequence"])
            else:
                break
        if position != checkpoint["sequence"]:
            tx.update("learning_checkpoints", {"source_id": source_id}, {"sequence": position})

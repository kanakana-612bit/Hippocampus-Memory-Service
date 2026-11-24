"""Multi-resolution memory acceptance with fictional text and isolated databases."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from v02.api import create_app
from v02.episode_contracts import raw_text_digest
from v02.service import Service
from v02.storage import Conflict, Missing


TMP = Path(__file__).resolve().parents[1] / "tmp-tests"
PREFIX = "/api/v0.2/episodes"
STAMP = "2026-09-23T01:00:00Z"


class EpisodeServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TMP / ("episode-service-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_fixture)
        env = patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.clocks = []
        for module in ("v02.episode_service", "v02.service", "v02.storage"):
            clock = patch(module + ".now", return_value=STAMP)
            self.clocks.append(clock.start())
            self.addCleanup(clock.stop)
        self.service = Service(self.folder / "demo.db", episode_mode="enabled")
        self.ep = self.service.episodes
        self.principal, self.host = "demo:owner", "demo:host"
        self.scope = {"conversation_id": "demo:chat", "context_key": "conversation", "sandbox_id": None}
        self.revision, self.serial = "r1", 0
        self.events = [
            {"id": "demo:u1", "role": "user", "actor_id": "demo:user",
             "content": "Fictional user: I choose the blue beacon.", "event_time": "2026-09-22T10:00:00Z"},
            {"id": "demo:u2", "role": "user", "actor_id": "demo:user",
             "content": "Fictional user: I will consider a green beacon later.", "event_time": "2026-09-22T10:05:00Z"},
            {"id": "demo:a1", "role": "assistant", "actor_id": "demo:model",
             "content": "Fictional assistant: The beacon test remains unresolved.", "event_time": "2026-09-22T10:06:00Z"},
        ]
        self.sync(self.events, [e["id"] for e in self.events], expected=None)

    def remove_fixture(self):
        target = self.folder.resolve()
        self.assertEqual(target.parent, TMP.resolve())
        self.assertTrue(target.name.startswith("episode-service-"))
        if target.exists():
            shutil.rmtree(target)

    def command(self, **values):
        self.serial += 1
        return {"idempotency_key": f"demo:command-{self.serial}", "reason": "Fictional episode acceptance",
                "scope": self.scope, "path_revision": self.revision, **values}

    def sync(self, messages, active, expected="r1", revision=None):
        revision = revision or self.revision
        return self.service.sync(self.principal, {
            "conversation_id": self.scope["conversation_id"], "messages": messages,
            "active_event_ids": active, "revision_key": revision, "expected_revision": expected,
            "auto_capture": False})

    def records(self, kind):
        with self.service.db.connection() as con:
            return self.service.db.list(con, self.principal, kind, self.scope["conversation_id"])

    def count(self):
        with self.service.db.connection() as con:
            return con.execute("SELECT count(*) FROM records").fetchone()[0]

    def source_span(self, event_id="demo:u1"):
        with self.service.db.connection() as con:
            record = self.service.db.get(con, self.principal, event_id, "event")
        text = record["data"]["content"]
        return {"event_ref": {"id": event_id, "version": record["version"]},
                "source_digest": raw_text_digest(text), "start": 0, "end": len(text), "quote": text}

    def prepare(self, operation="provisional", **values):
        return self.ep.prepare(self.principal, self.host, self.command(operation=operation, **values))

    def commit_body(self, job, proposal):
        self.serial += 1
        return {"idempotency_key": f"demo:commit-{self.serial}", "reason": "Fictional generated derivation",
                "manifest_digest": job["manifest_digest"], "proposal": proposal,
                "generator": {"model_id": "demo:model", "prompt_version": "demo:v1", "algorithm_version": "demo:v1"}}

    def commit(self, job, proposal):
        return self.ep.commit(self.principal, self.host, job["job_id"], self.commit_body(job, proposal))

    def provisional_job(self):
        self.ep.seal(self.principal, self.command())
        job = self.prepare()
        summaries = []
        for chunk in job["input_bundle"]["chunks"]:
            spans = [self.source_span(i) for i in chunk["source_event_ids"]]
            user_spans = [s for s in spans if s["event_ref"]["id"].startswith("demo:u")]
            summaries.append({"chunk_ref": chunk["ref"], "summary": "Fictional beacon choices and test discussion.",
                "source_spans": spans, "topics": ["beacon"],
                "decision_candidates": [{"text": "The fictional user chose a blue beacon.",
                                         "source_spans": user_spans[:1]}] if user_spans else []})
        return job, {"summaries": summaries}

    def provisional(self):
        job, proposal = self.provisional_job()
        return self.commit(job, proposal)

    def daily_proposal(self, summary_refs, *, relation=None):
        leaf = {"local_id": "local-left", "level": "local", "title": "Fictional beacon choice",
                "summary": "Fictional beacon choice summary.", "child_refs": summary_refs}
        if relation:
            leaf["relations"] = [{"kind": "continuation", "target_ref": relation}]
        return {"episodes": [leaf,
            {**deepcopy(leaf), "local_id": "local-right", "title": "Fictional beacon testing"},
            {"local_id": "theme", "level": "theme", "title": "Fictional beacon project",
             "summary": "Fictional beacon project across related contexts.",
             "child_local_ids": ["local-left", "local-right"]}]}

    def daily(self):
        provisional = self.provisional()
        job = self.prepare("daily", day="2026-09-22")
        result = self.commit(job, self.daily_proposal(provisional["summary_refs"]))
        return provisional, result

    def query(self, **values):
        return self.ep.query(self.principal, {"scope": self.scope, "path_revision": self.revision,
                                            "query": "", **values})

    def test_provisional_preserves_originals_weights_and_candidate_only_status(self):
        originals = deepcopy(self.records("event"))
        result = self.provisional()
        self.assertFalse(result["raw_changed"])
        self.assertFalse(result["knowledge_promoted"])
        self.assertEqual(self.records("event"), originals)
        self.assertEqual(self.records("knowledge_claim"), [])
        for record in self.records("episode_summary") + self.records("structured_candidate"):
            d = record["data"]
            self.assertEqual((d["retrieval_weight"], d["salience"], d["generalization_weight"]), (0.5, 0.5, 0.0))
            self.assertIsNone(d["assessment"]["score"])
            self.assertEqual(d["assessment"]["status"], "unknown")
        self.assertEqual(self.query(mode="structured")["structured"], [])
        self.assertEqual(self.query(mode="structured", include_history=True)["structured"][0]["adoption"], "candidate")

    def test_daily_overlapping_dag_high_level_then_children_then_raw(self):
        _, result = self.daily()
        self.assertEqual(len(result["episode_refs"]), 3)
        top = self.query(mode="episodic")["episodes"]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["level"], "theme")
        children = self.query(mode="episodic", target_ref=top[0]["ref"])["episodes"]
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]["source_event_ids"], children[1]["source_event_ids"])
        raw = self.query(mode="raw", target_ref=children[0]["ref"])["sources"]
        self.assertEqual([s["event_ref"]["id"] for s in raw], [e["id"] for e in self.events])
        self.assertEqual(raw[0]["content"], self.events[0]["content"])

    def test_daily_continuation_crosses_days_without_replacing_history(self):
        _, first = self.daily()
        historical = deepcopy(self.records("episode"))
        later = {"id": "demo:u3", "role": "user", "content": "Fictional beacon follow-up next day.",
                 "event_time": "2026-09-23T10:00:00Z"}
        for clock in self.clocks:
            clock.return_value = "2026-09-24T01:00:00Z"
        self.sync([later], [e["id"] for e in self.events] + [later["id"]], revision="r2")
        self.revision = "r2"
        next_provisional = self.provisional()
        next_job = self.prepare("daily", day="2026-09-23")
        self.commit(next_job, self.daily_proposal(next_provisional["summary_refs"], relation=first["episode_refs"][0]))
        with self.service.db.connection() as con:
            for record in historical:
                self.assertEqual(self.service.db.get(con, self.principal, record["id"]), record)
        self.assertTrue(any(r["data"]["relations"] for r in self.records("episode") if r not in historical))

    def test_daily_refuses_unfinished_local_day(self):
        self.provisional()
        with self.assertRaises(ValueError):
            self.prepare("daily", day="2026-09-23")

    def test_review_is_fidelity_only_and_same_clock_cas_history_is_ordered(self):
        candidate = self.provisional()["candidate_refs"][0]
        confirm = self.ep.review(self.principal, self.host, self.command(target_ref=candidate, action="confirm"))
        self.assertEqual(confirm["data"]["confirmation_target"], "fidelity_to_explicit_speech_not_world_truth")
        self.assertEqual(self.query(mode="structured")["structured"][0]["adoption"], "current")
        with self.assertRaises(Conflict):
            self.ep.review(self.principal, self.host, self.command(target_ref=candidate, action="withdraw"))
        previous = {"id": confirm["id"], "version": confirm["version"]}
        for action in ("withdraw", "confirm", "withdraw", "confirm"):
            review = self.ep.review(self.principal, self.host,
                self.command(target_ref=candidate, action=action, expected_review_ref=previous))
            previous = {"id": review["id"], "version": review["version"]}
            items = self.query(mode="structured", include_history=True)["structured"]
            self.assertEqual(items[0]["adoption"], "current" if action == "confirm" else "withdrawn")
        self.assertEqual(len(self.records("structured_review")), 5)
        self.assertEqual(self.records("structured_candidate")[0]["version"], 1)

    def test_supersession_does_not_automatically_confirm_replacement(self):
        job, proposal = self.provisional_job()
        proposal["structured_candidates"] = [{"kind": "user_decision", "subject": "user", "topic": "beacon",
            "text": "Fictional replacement choice awaits review.", "source_spans": [self.source_span("demo:u2")]}]
        candidates = self.commit(job, proposal)["candidate_refs"]
        old = candidates[1]  # Extracted summary candidate follows explicit candidates.
        replacement = candidates[0]
        self.ep.review(self.principal, self.host, self.command(target_ref=old, action="supersede", replacement_ref=replacement))
        self.assertEqual(self.query(mode="structured")["structured"], [])
        statuses = {r["ref"]["id"]: r["adoption"] for r in self.query(mode="structured", include_history=True)["structured"]}
        self.assertEqual(statuses[old["id"]], "superseded")
        self.assertEqual(statuses[replacement["id"]], "candidate")
        with self.assertRaises(ValueError):
            self.ep.review(self.principal, self.host,
                self.command(target_ref=replacement, action="supersede", replacement_ref=old))

    def test_unresolved_is_lifecycle_state_without_weight_or_truth_change(self):
        job, proposal = self.provisional_job()
        proposal["structured_candidates"] = [{"kind": "unresolved", "subject": "conversation", "topic": "beacon test",
            "text": "Fictional beacon test is unresolved.", "source_spans": [self.source_span("demo:a1")]}]
        target = self.commit(job, proposal)["candidate_refs"][0]
        self.ep.review(self.principal, self.host,
            self.command(target_ref=target, action="confirm", unresolved_status="deferred"))
        current = self.query(mode="structured")["structured"][0]
        self.assertEqual(current["unresolved_status"], "deferred")
        self.assertEqual(current["generalization_weight"], 0.0)
        self.assertIsNone(current["assessment"]["score"])

    def test_wrong_quote_rejects_atomically_without_consuming_job(self):
        job, proposal = self.provisional_job()
        bad = deepcopy(proposal)
        bad["summaries"][0]["source_spans"][-1]["quote"] = "x" * len(bad["summaries"][0]["source_spans"][-1]["quote"])
        before = self.count()
        with self.assertRaises(ValueError):
            self.commit(job, bad)
        self.assertEqual(self.count(), before)
        self.assertEqual(self.records("episode_summary"), [])
        self.assertEqual(len(self.commit(job, proposal)["summary_refs"]), 1)

    def test_assistant_statement_cannot_become_user_decision_even_after_summary_write(self):
        job, proposal = self.provisional_job()
        proposal["structured_candidates"] = [{"kind": "user_decision", "subject": "user", "topic": "beacon",
            "text": "Fictional assistant claim must not be attributed to user.", "source_spans": [self.source_span("demo:a1")]}]
        before = self.count()
        with self.assertRaises(ValueError):
            self.commit(job, proposal)
        self.assertEqual(self.count(), before)
        self.assertEqual(self.records("episode_summary"), [])

    def test_stale_path_commit_is_rejected_and_branch_raw_query_isolated(self):
        job, proposal = self.provisional_job()
        self.sync([], ["demo:u2", "demo:a1"], revision="r2")
        self.revision = "r2"
        before = self.count()
        with self.assertRaises(Conflict):
            self.commit(job, proposal)
        self.assertEqual(self.count(), before)
        raw = self.query(mode="raw")["sources"]
        self.assertNotIn("demo:u1", [r["event_ref"]["id"] for r in raw])

    def test_commit_is_bound_to_owner_host_and_manifest(self):
        job, proposal = self.provisional_job()
        body = self.commit_body(job, proposal)
        before = self.count()
        with self.assertRaises(Missing):
            self.ep.commit("demo:other-owner", self.host, job["job_id"], body)
        with self.assertRaises(Conflict):
            self.ep.commit(self.principal, "demo:other-host", job["job_id"], body)
        with self.assertRaises(Conflict):
            self.ep.commit(self.principal, self.host, job["job_id"], {**body, "manifest_digest": "0" * 64})
        self.assertEqual(self.count(), before)

    def test_context_and_sandbox_sources_cannot_be_reclassified(self):
        sandbox = {"id": "demo:fiction", "role": "user", "content": "Fictional nested story beacon.",
                   "sandbox_id": "demo:story", "event_time": "2026-09-22T10:07:00Z"}
        self.sync([sandbox], [e["id"] for e in self.events] + [sandbox["id"]], revision="r2")
        self.revision = "r2"
        job, proposal = self.provisional_job()
        proposal["summaries"][0]["source_spans"].append(self.source_span("demo:fiction"))
        with self.assertRaises((Conflict, ValueError)):
            self.commit(job, proposal)
        other_context = self.ep.query(self.principal, {"scope": {**self.scope, "context_key": "different"},
            "path_revision": self.revision, "mode": "raw"})
        self.assertEqual(other_context["sources"], [])

    def test_stale_source_revision_is_rejected(self):
        job, proposal = self.provisional_job()
        proposal["summaries"][0]["source_spans"][0]["event_ref"]["version"] = 2
        with self.assertRaises(Conflict):
            self.commit(job, proposal)
        self.assertEqual(self.records("episode_summary"), [])

    def test_valid_path_source_outside_job_bundle_is_rejected(self):
        self.provisional()
        later = {"id": "demo:u3", "role": "user", "content": "Fictional second beacon conversation.",
                 "event_time": "2026-09-22T11:00:00Z"}
        self.sync([later], [e["id"] for e in self.events] + [later["id"]], revision="r2")
        self.revision = "r2"
        job, proposal = self.provisional_job()
        self.assertEqual(len(job["input_bundle"]["chunks"]), 1)
        proposal["summaries"][0]["source_spans"].append(self.source_span("demo:u1"))
        before = self.count()
        with self.assertRaises(ValueError):
            self.commit(job, proposal)
        self.assertEqual(self.count(), before)

    def test_structured_review_invalidates_previously_compiled_run(self):
        provisional, _ = self.daily()
        run = self.service.build_run(self.principal, {"conversation_id": self.scope["conversation_id"],
            "path_revision": self.revision, "query": "beacon", "idempotency_key": "demo:run"})
        self.assertEqual(run["data"]["packet"]["version"], "hippocampus.packet.v0.4")
        self.assertEqual(run["data"]["packet"]["episodic_recall"]["structured"], [])
        with self.service.db.connection() as con:
            self.service.validate_run(con, self.principal, run)
        self.ep.review(self.principal, self.host,
            self.command(target_ref=provisional["candidate_refs"][0], action="confirm"))
        with self.service.db.connection() as con, self.assertRaises(Conflict):
            self.service.validate_run(con, self.principal, run)
        fresh = self.service.build_run(self.principal, {"conversation_id": self.scope["conversation_id"],
            "path_revision": self.revision, "query": "beacon", "idempotency_key": "demo:fresh-run"})
        self.assertEqual(fresh["data"]["packet"]["episodic_recall"]["structured"][0]["adoption"], "current")

    def test_shadow_selection_is_distinct_from_packet_and_mode_change_invalidates_run(self):
        self.daily()
        self.service.episode_mode = "shadow"
        run = self.service.build_run(self.principal, {"conversation_id": self.scope["conversation_id"],
            "path_revision": self.revision, "query": "beacon", "idempotency_key": "demo:shadow-run"})
        self.assertTrue(run["data"]["episode_selection"]["episodes"])
        self.assertEqual(run["data"]["compiled_episode_refs"], [])
        self.assertNotIn("episodic_recall", run["data"]["packet"])
        self.assertEqual(run["data"]["packet"]["version"], "hippocampus.packet.v0.3")
        self.service.episode_mode = "enabled"
        with self.service.db.connection() as con, self.assertRaises(Conflict):
            self.service.validate_run(con, self.principal, run)

    def test_context_scope_filters_recent_raw_and_legacy_memory_origins(self):
        foreign = {"id": "demo:other-context", "role": "user", "context_key": "demo:separate-context",
                   "content": "Fictional beacon detail in a separate context.", "event_time": "2026-09-22T10:07:00Z"}
        self.sync([foreign], [e["id"] for e in self.events] + [foreign["id"]], revision="r2")
        self.revision = "r2"
        parent = self.service.create_memory(self.principal, {
            "idempotency_key": "demo:other-context-memory", "reason": "Fictional legacy source-bound memory",
            "memory": {"conversation_id": self.scope["conversation_id"], "content": "Fictional beacon memory.",
                       "source_event_ids": [foreign["id"]]}})
        child = self.service.create_memory(self.principal, {
            "idempotency_key": "demo:other-context-derived", "reason": "Fictional legacy descendant",
            "memory": {"conversation_id": self.scope["conversation_id"], "content": "Fictional beacon descendant.",
                       "source_memory_ids": [parent["id"]]}})
        run = self.service.build_run(self.principal, {"conversation_id": self.scope["conversation_id"],
            "path_revision": self.revision, "context_key": "conversation", "query": "beacon",
            "idempotency_key": "demo:context-isolation-run"})
        packet = run["data"]["packet"]
        self.assertNotIn(foreign["id"], [e["id"] for e in packet["source_evidence"]])
        self.assertFalse({parent["id"], child["id"]} & {m["id"] for m in packet["derived_memories"]})

    def test_commit_retry_is_idempotent_different_proposal_is_not(self):
        job, proposal = self.provisional_job()
        body = self.commit_body(job, proposal)
        result = self.ep.commit(self.principal, self.host, job["job_id"], body)
        before = self.count()
        self.assertEqual(self.ep.commit(self.principal, self.host, job["job_id"], body), result)
        self.assertEqual(self.count(), before)
        other = deepcopy(proposal)
        other["summaries"][0]["summary"] = "Different fictional derivation."
        with self.assertRaises(Conflict):
            self.commit(job, other)
        self.assertEqual(self.count(), before)

    def test_regeneration_appends_replacement_preserves_old_episode_and_raw(self):
        _, result = self.daily()
        original_events = deepcopy(self.records("event"))
        old_top = self.query(mode="episodic")["episodes"][0]
        job = self.prepare("regenerate", source_refs=[old_top["ref"]])
        proposal = {"episodes": [{"local_id": "replacement", "level": "theme", "title": "Revised fictional beacon",
            "summary": "Corrected fictional beacon project summary.", "child_refs": old_top["child_refs"],
            "supersedes_ref": old_top["ref"]}]}
        created = self.commit(job, proposal)["episode_refs"][0]
        self.assertEqual(self.query(mode="episodic")["episodes"][0]["ref"], created)
        history = self.query(mode="episodic", include_history=True)["episodes"]
        self.assertIn(old_top["ref"], [r["ref"] for r in history])
        self.assertEqual(self.records("event"), original_events)
        self.assertEqual(len(self.records("episode")), 4)

    def test_artifact_pointer_is_external_authority_and_never_current_value(self):
        original = self.ep.artifact(self.principal, self.host, self.command(title="Fictional beacon implementation",
            uri="https://example.invalid/demo-repository", kind="git_repository", source_spans=[self.source_span()]))
        self.assertFalse(original["data"]["external_content_fetched"])
        current = self.query(mode="artifact")
        self.assertFalse(current["current_state_verified"])
        self.assertTrue(current["artifacts"][0]["requires_authorized_refresh"])
        self.ep.artifact(self.principal, self.host, self.command(title="Fictional beacon implementation moved",
            uri="https://example.invalid/demo-repository-v2", kind="git_repository", source_spans=[self.source_span()],
            supersedes_ref={"id": original["id"], "version": original["version"]}))
        self.assertEqual(len(self.query(mode="artifact")["artifacts"]), 1)
        self.assertEqual(len(self.query(mode="artifact", include_history=True)["artifacts"]), 2)

    def test_raw_search_is_literal_and_read_only(self):
        special = {"id": "demo:literal", "role": "user", "content": "架空の原文 100%_demo のみ。",
                   "event_time": "2026-09-22T10:07:00Z"}
        self.sync([special], [e["id"] for e in self.events] + [special["id"]], revision="r2")
        self.revision = "r2"
        before = self.count()
        self.assertEqual([s["event_ref"]["id"] for s in self.query(mode="raw", query="原文")["sources"]], [special["id"]])
        self.assertEqual([s["event_ref"]["id"] for s in self.query(mode="raw", query="%_demo")["sources"]], [special["id"]])
        self.assertEqual(self.count(), before)
        self.assertTrue(self.service.db.verify()["valid"])

    def test_no_work_polls_do_not_manufacture_records(self):
        self.provisional()
        before = self.count()
        for _ in range(3):
            self.assertEqual(self.prepare()["status"], "no_work")
            result = self.ep.seal(self.principal, self.command())
            self.assertEqual(result["chunk_refs"], [])
        self.assertEqual(self.count(), before)

    def test_api_permissions_separate_read_write_and_review(self):
        credentials = [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "principal_id": self.principal,
                        "host_id": self.host, "permissions": permissions}
            for token, permissions in (("demo-reader", ["read"]), ("demo-writer", ["write", "read"]),
                                       ("demo-reviewer", ["review", "read"]))]
        with TestClient(create_app(self.service, credentials)) as client:
            def post(path, body, token):
                return client.post(PREFIX + path, json=body, headers={"Authorization": "Bearer " + token})
            query = {"scope": self.scope, "path_revision": self.revision, "mode": "raw", "query": ""}
            self.assertEqual(post("/query", query, "demo-reader").status_code, 200)
            self.assertEqual(post("/chunks/seal", self.command(), "demo-reader").status_code, 403)
            self.assertEqual(post("/chunks/seal", self.command(), "demo-writer").status_code, 200)
            candidate = self.provisional()["candidate_refs"][0]
            review = self.command(target_ref=candidate, action="confirm")
            self.assertEqual(post("/structured/reviews", review, "demo-writer").status_code, 403)
            self.assertEqual(post("/structured/reviews", review, "demo-reviewer").status_code, 200)
            artifact = self.command(title="Fictional design", uri="https://example.invalid/design", kind="design_document",
                                    source_spans=[self.source_span()])
            self.assertEqual(post("/artifacts", artifact, "demo-writer").status_code, 403)
            self.assertEqual(post("/artifacts", artifact, "demo-reviewer").status_code, 200)
            self.assertEqual(client.post(PREFIX + "/query", json=query).status_code, 401)


if __name__ == "__main__":
    unittest.main()

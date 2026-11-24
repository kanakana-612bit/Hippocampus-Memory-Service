"""K1 integration using fictional events, credentials, and isolated temporary DBs."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from v02.api import Identity, create_app
from v02.contracts import digest, uid
from v02.service import Service
from v02.storage import Missing


ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp-tests"
STAMP = "2026-01-01T10:00:00Z"
DUE = "2026-01-02T10:00:00Z"
PREFIX = "/api/v0.2"
KNOWLEDGE = PREFIX + "/knowledge"


class KnowledgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TMP / ("knowledge-service-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_fixture)
        self.clock = patch("v02.knowledge_service.now", return_value=STAMP)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        for module in ("v02.countersearch_service", "v02.knowledge_observations"):
            clock = patch(module + ".now", return_value=STAMP)
            clock.start()
            self.addCleanup(clock.stop)
        self.environment = patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = Service(self.folder / "demo.db")
        self.principal = "demo:owner"
        self.identity = Identity(self.principal, "demo:host", frozenset({"read", "write", "review", "sync"}))
        self.serial = 0
        credentials = []
        for token, principal, permissions in (
            ("demo-reviewer", self.principal, ["read", "write", "review", "sync"]),
            ("demo-writer", self.principal, ["read", "write", "sync"]),
            ("demo-reader", self.principal, ["read"]),
            ("demo-other-owner", "demo:other-owner", ["read", "write", "review", "sync"]),
        ):
            credentials.append({"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                "principal_id": principal, "host_id": "demo:host", "permissions": permissions})
        self.client = TestClient(create_app(self.service, credentials))
        self.addCleanup(self.client.close)
        self.events = [
            {"id": "demo:user", "role": "user", "actor_id": "demo:narrator",
             "content": "Demo prefix. The beacon appears blue. Demo suffix."},
            {"id": "demo:tool", "role": "tool", "actor_id": "demo:instrument",
             "content": "Independent demo instrument recorded blue."},
            {"id": "demo:assistant", "role": "assistant", "actor_id": "demo:model",
             "content": "Generated demo claim: the beacon appears blue."},
            {"id": "demo:fiction", "role": "user", "actor_id": "demo:narrator", "sandbox_id": "demo:story",
             "content": "Inside the demo story, the beacon appears blue."},
        ]
        self.active = [item["id"] for item in self.events]
        self.sync(self.events, self.active)

    def remove_fixture(self):
        target = self.folder.resolve()
        self.assertEqual(target.parent, TMP.resolve())
        self.assertTrue(target.name.startswith("knowledge-service-"))
        if target.exists():
            shutil.rmtree(target)

    def call(self, method, url, *, body=None, token="demo-reviewer", status=200):
        headers = {"Authorization": "Bearer " + token} if token else {}
        response = self.client.request(method, url, headers=headers, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def sync(self, messages, active, *, conversation="demo:chat", revision="r1", expected=None, principal=None):
        return self.service.sync(principal or self.principal, {
            "conversation_id": conversation, "messages": messages, "active_event_ids": active,
            "revision_key": revision, "expected_revision": expected, "auto_capture": False})

    def command(self, *, revision="r1", **values):
        self.serial += 1
        return {"idempotency_key": f"demo:command-{self.serial}", "reason": "Fictional manual review test",
                "path_revision": revision, **values}

    def ack_ref(self, result, kind):
        self.assertEqual(set(result), {"result_semantics", "current_applicability", "records"})
        self.assertEqual(result["result_semantics"], "recorded_revisions_only")
        self.assertEqual(result["current_applicability"], "not_evaluated")
        for item in result["records"]:
            self.assertEqual(set(item), {"id", "version", "kind"})
        record = next(item for item in result["records"] if item["kind"] == kind)
        return {"id": record["id"], "version": record["version"]}

    def source(self, event="demo:user", *, conversation="demo:chat", sandbox=None, parents=(), token="demo-reviewer"):
        request = self.command(scope={"conversation_id": conversation, "sandbox_id": sandbox},
                               event_ref={"id": event, "version": 1}, parent_source_refs=list(parents))
        result = self.call("POST", KNOWLEDGE + "/sources", body=request, token=token)
        return self.ack_ref(result, "knowledge_source")

    def proposition(self):
        return {"text": "The demo narrator reported a blue beacon.", "kind": "attributed_report",
                "subject": "demo:narrator", "predicate": "reported_beacon_colour", "object": "blue",
                "polarity": "affirmed", "quantifier": "single", "attribution_actor_id": "demo:narrator",
                "conditions": ["Within the demo observation record"],
                "valid_from": "2025-12-01T00:00:00Z", "valid_until": "2025-12-02T00:00:00Z"}

    def create_claim(self, source=None, *, sandbox=None, proposition=None):
        source = source or self.source(sandbox=sandbox)
        request = self.command(scope={"conversation_id": "demo:chat", "sandbox_id": sandbox},
                               proposition=proposition or self.proposition(), origin_refs=[source])
        result = self.call("POST", KNOWLEDGE + "/claims", body=request)
        return self.ack_ref(result, "knowledge_claim")["id"], source

    def claim(self, identity):
        return self.service.knowledge.get_claim(self.identity, identity)

    def claim_command(self, identity, *, revision="r1", **values):
        record = self.claim(identity)
        return self.command(revision=revision, expected_version=record["version"],
                            claim_revision=record["data"]["claim_revision"],
                            evidence_set_revision=record["data"]["evidence_set_revision"], **values)

    def evidence_spec(self, source, **changes):
        return {"source_ref": source, "relation": "supports", "content_check": "matched",
                "source_kind": "primary_report", "origin_status": "known", "span_start": 13,
                "span_end": 37, "review_reason": "Demo narrator text matched the attributed report", **changes}

    def replace(self, identity, specifications):
        result = self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence",
                           body=self.claim_command(identity, evidence=specifications))
        self.ack_ref(result, "knowledge_claim")
        return [{"id": item["id"], "version": item["version"]}
                for item in result["records"] if item["kind"] == "knowledge_evidence"]

    def search(self, identity, **changes):
        queries = [{"purpose": purpose, "text": text, "terms": terms,
                    "reason": "Inspect the supplied fictional corpus for this falsifying condition"}
                   for purpose, text, terms in (
                       ("falsification", "Find observations reporting another colour", ["red", "green"]),
                       ("alternative_explanation", "Find an instrument fault", ["fault", "calibration"]),
                       ("boundary_case", "Find conditions outside the reported observation", ["outside", "except"]),
                       ("source_correction", "Find a correction to the original report", ["correction", "withdrawn"]))]
        request = self.claim_command(identity, queries=queries, **changes)
        result = self.call("POST", KNOWLEDGE + f"/claims/{identity}/search-plans", body=request)
        plan = self.ack_ref(result, "knowledge_search_plan")
        result = self.call("POST", KNOWLEDGE + f"/search-plans/{plan['id']}/execute", body=self.command())
        return self.ack_ref(result, "knowledge_search")

    def review_request(self, identity, evidence, search, *, action="adopt", revision="r1", **changes):
        claim = self.claim(identity)["data"]
        assessment = {"target": "world_claim", "claim": claim["proposition"]["text"], "score": .7,
                      "status": "assessed", "evidence_refs": [item["id"] for item in evidence]}
        values = {"action": action, "profile": "attributed_report", "assessment": assessment,
            "search_refs": [search] if search else [], "counterevidence_status": "none_found_in_scope",
            "source_independence": "unknown", "review_after": DUE, **changes}
        return self.claim_command(identity, revision=revision, **values)

    def review(self, identity, evidence, search, **changes):
        request = self.review_request(identity, evidence, search, **changes)
        result = self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request)
        self.ack_ref(result, "knowledge_claim")
        return request, self.ack_ref(result, "knowledge_review")

    def prepared(self):
        identity, source = self.create_claim()
        evidence = self.replace(identity, [self.evidence_spec(source)])
        search = self.search(identity)
        return identity, source, evidence, search

    def counts(self):
        with self.service.db.connection() as con:
            return {row["kind"]: row["n"] for row in con.execute("SELECT kind, COUNT(*) n FROM records GROUP BY kind")}

    def test_full_manual_lifecycle_preserves_withdrawal_and_revision_history(self):
        identity, source, evidence, search = self.prepared()
        initial = self.claim(identity)
        self.assertEqual(initial["data"]["knowledge_state"], "provisional")
        self.assertEqual(initial["data"]["assessment"]["status"], "unknown")
        self.assertEqual(initial["data"]["evidence_set_revision"], 2)
        self.review(identity, evidence, search)
        adopted = self.claim(identity)
        self.assertEqual(adopted["data"]["knowledge_state"], "established")
        self.assertEqual(adopted["current_applicability"], "not_evaluated")
        _, withdrawn = self.review(identity, evidence, search, action="withdraw")
        self.review(identity, evidence, search, action="hold")
        self.assertEqual(self.claim(identity)["data"]["withdrawal_review_ref"], withdrawn)
        failed = self.review_request(identity, evidence, search)
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=failed, status=422)
        search = self.search(identity)
        self.review(identity, evidence, search, reopens_withdrawal_ref=withdrawn)
        before = self.claim(identity)
        changed = deepcopy(before["data"]["proposition"])
        changed.update(text="The demo narrator reported a red beacon.", object="red")
        result = self.call("PATCH", KNOWLEDGE + f"/claims/{identity}",
                           body=self.claim_command(identity, proposition=changed))
        self.ack_ref(result, "knowledge_claim")
        after = self.claim(identity)
        self.assertEqual(after["data"]["claim_revision"], before["data"]["claim_revision"] + 1)
        self.assertEqual(after["data"]["evidence_set_revision"], before["data"]["evidence_set_revision"] + 1)
        self.assertEqual(after["data"]["assessment"]["status"], "unknown")
        self.assertIsNone(after["data"]["assessment"]["score"])
        self.assertIsNone(after["data"]["latest_review_ref"])
        self.assertEqual(after["data"]["withdrawal_review_ref"], withdrawn)
        history = self.call("GET", KNOWLEDGE + f"/claims/{identity}/history")
        self.assertEqual(len(history["versions"]), after["version"] - 1)
        self.assertTrue(all(item["interpretation"] == "historical_snapshot" for item in history["versions"]))
        self.assertTrue(self.service.db.verify()["valid"])

    def test_evidence_replacement_invalidates_assessment_without_changing_proposition(self):
        identity, source, evidence, search = self.prepared()
        self.review(identity, evidence, search)
        before = self.claim(identity)
        self.replace(identity, [self.evidence_spec(source, span_start=0, span_end=12)])
        after = self.claim(identity)
        self.assertEqual(after["data"]["claim_revision"], before["data"]["claim_revision"])
        self.assertEqual(after["data"]["evidence_set_revision"], before["data"]["evidence_set_revision"] + 1)
        self.assertEqual(after["data"]["knowledge_state"], "provisional")
        self.assertEqual(after["data"]["counterevidence_status"], "unassessed")
        self.assertEqual(after["data"]["assessment"]["status"], "unknown")
        self.assertEqual(len(self.service.list_records(self.principal, "knowledge_evidence")), 2)

    def test_edits_and_empty_evidence_replacement_preserve_change_provenance_history(self):
        identity, _ = self.create_claim()
        original = deepcopy(self.claim(identity)["data"]["provenance"])
        changed = self.proposition()
        changed.update(text="The demo narrator reported a red beacon.", object="red")
        edit_time = "2026-01-01T10:01:00Z"
        empty_time = "2026-01-01T10:02:00Z"
        edit_reason = "Demo reviewed correction to the reported colour"
        empty_reason = "Demo previous evidence no longer supports the corrected claim"
        with patch("v02.knowledge_service.now", return_value=edit_time):
            request = self.claim_command(identity, proposition=changed)
            request["reason"] = edit_reason
            self.call("PATCH", KNOWLEDGE + f"/claims/{identity}", body=request)
        edited = deepcopy(self.claim(identity)["data"]["provenance"])
        with patch("v02.knowledge_service.now", return_value=empty_time):
            request = self.claim_command(identity, evidence=[])
            request["reason"] = empty_reason
            self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence", body=request)
        current = self.claim(identity)["data"]["provenance"]
        for provenance, target, stamp, reason in (
            (edited, "claim_change", edit_time, edit_reason),
            (current, "evidence_set_change", empty_time, empty_reason),
        ):
            with self.subTest(target=target):
                self.assertEqual(provenance["source"], "authorized_host:demo:host")
                self.assertEqual(provenance["reason"], reason)
                self.assertEqual(provenance["created_at"], stamp)
                self.assertEqual(provenance["confidence_target"], target)
                self.assertIsNone(provenance["confidence"])
                self.assertIn(identity, provenance["cause_refs"])
        history = self.call("GET", KNOWLEDGE + f"/claims/{identity}/history")
        versions = {item["data"]["version"]: item["data"]["snapshot"] for item in history["versions"]}
        self.assertEqual(versions[1]["provenance"], original)
        self.assertEqual(versions[2]["provenance"], edited)
        self.assertEqual(history["current"]["data"]["provenance"], current)
        self.assertEqual(self.service.list_records(self.principal, "knowledge_evidence"), [])

    def test_idempotent_replay_is_revision_ack_even_after_withdrawal_and_cas_rejects_stale(self):
        identity, _, evidence, search = self.prepared()
        request = self.review_request(identity, evidence, search)
        original = self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request)
        self.review(identity, evidence, search, action="withdraw")
        before = self.counts()
        replay = self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request)
        self.assertEqual(original, replay)
        self.ack_ref(replay, "knowledge_review")
        self.assertEqual(self.counts(), before)
        changed = {**request, "reason": "Different request cannot reuse a receipt"}
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=changed, status=409)
        changed = {**request, "idempotency_key": "demo:new-key-with-old-versions"}
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=changed, status=409)
        receipt = self.service.get(self.principal, uid("receipt", self.principal, "knowledge.claim.review", request["idempotency_key"]))
        self.assertEqual(receipt["data"]["result"], original)
        self.assertNotIn("eligible", str(receipt["data"]["result"]))

    def test_partial_evidence_write_rolls_back_records_receipt_and_claim_version(self):
        identity, source = self.create_claim()
        before = self.counts()
        claim_before = self.claim(identity)
        request = self.claim_command(identity, evidence=[self.evidence_spec(source), self.evidence_spec(source, span_end=10000)])
        self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence", body=request, status=422)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.claim(identity), claim_before)
        with self.assertRaises(Missing):
            self.service.get(self.principal, uid("receipt", self.principal, "knowledge.evidence.replace", request["idempotency_key"]))
        self.assertTrue(self.service.db.verify()["valid"])

    def test_sources_cannot_cross_principal_conversation_or_selected_branch(self):
        source = self.source()
        request = self.command(scope={"conversation_id": "demo:chat"}, event_ref={"id": "demo:user", "version": 1})
        self.call("POST", KNOWLEDGE + "/sources", body=request, token="demo-other-owner", status=404)
        self.sync([{"id": "demo:other-conversation-event", "role": "user", "content": "Other fictional context"}],
                  ["demo:other-conversation-event"], conversation="demo:other-chat")
        request = self.command(scope={"conversation_id": "demo:other-chat"}, event_ref={"id": "demo:user", "version": 1})
        self.call("POST", KNOWLEDGE + "/sources", body=request, status=422)
        self.sync([], ["demo:tool"], revision="r2", expected="r1")
        request = self.command(revision="r2", scope={"conversation_id": "demo:chat"},
                               proposition=self.proposition(), origin_refs=[source])
        self.call("POST", KNOWLEDGE + "/claims", body=request, status=422)
        request = self.command(scope={"conversation_id": "demo:chat"}, event_ref={"id": "demo:tool", "version": 1})
        self.call("POST", KNOWLEDGE + "/sources", body=request, status=409)

    def test_off_path_claim_cannot_be_adopted_but_can_be_retired(self):
        identity, _, evidence, search = self.prepared()
        self.sync([], ["demo:tool"], revision="r2", expected="r1")
        request = self.review_request(identity, evidence, search, revision="r2")
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        self.review(identity, evidence, search, action="withdraw", revision="r2")
        self.assertEqual(self.claim(identity)["data"]["knowledge_state"], "withdrawal_candidate")

    def test_fiction_scope_cannot_relabel_world_events_sources_or_evidence(self):
        world = self.source()
        request = self.command(scope={"conversation_id": "demo:chat", "sandbox_id": "demo:story"},
                               event_ref={"id": "demo:user", "version": 1})
        self.call("POST", KNOWLEDGE + "/sources", body=request, status=422)
        story = self.source("demo:fiction", sandbox="demo:story")
        identity, _ = self.create_claim(story, sandbox="demo:story")
        self.assertEqual(self.claim(identity)["data"]["scope"]["domain"], "fiction")
        request = self.claim_command(identity, evidence=[self.evidence_spec(world)])
        self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence", body=request, status=422)
        request = self.command(scope={"conversation_id": "demo:chat"}, proposition=self.proposition(), origin_refs=[story])
        self.call("POST", KNOWLEDGE + "/claims", body=request, status=422)

    def test_assistant_and_derived_source_cannot_be_laundered_into_primary_evidence(self):
        identity, _ = self.create_claim()
        assistant = self.source("demo:assistant")
        for kind in ("primary_report", "direct_observation", "checked_derivation"):
            with self.subTest(kind=kind):
                request = self.claim_command(identity, evidence=[self.evidence_spec(assistant, source_kind=kind)])
                self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence", body=request, status=422)
        evidence = self.replace(identity, [self.evidence_spec(assistant, source_kind="model_output")])
        stored = self.service.get(self.principal, evidence[0]["id"])
        self.assertEqual(stored["data"]["snapshot"]["source_kind"], "model_output")
        derived = self.source(parents=[assistant])
        request = self.claim_command(identity, evidence=[self.evidence_spec(derived, source_kind="direct_observation")])
        self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence", body=request, status=422)

    def test_same_event_source_duplicates_cannot_be_certified_independent(self):
        first, second = self.source(), self.source()
        self.assertNotEqual(first["id"], second["id"])
        proposition = self.proposition()
        proposition.update(kind="empirical", quantifier="bounded")
        identity, _ = self.create_claim(first, proposition=proposition)
        evidence = self.replace(identity, [self.evidence_spec(item, source_kind="direct_observation") for item in (first, second)])
        search = self.search(identity)
        request = self.review_request(identity, evidence, search, profile="independent_observations", source_independence="independent",
            independence_pairs=[{"evidence_ids": [item["id"] for item in evidence], "status": "independent", "reason": "Demo invalid assertion"}])
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        self.assertEqual(self.claim(identity)["data"]["knowledge_state"], "provisional")
        snapshots = [self.service.get(self.principal, item["id"])["data"]["snapshot"] for item in evidence]
        self.assertEqual(snapshots[0]["origin_ids"], snapshots[1]["origin_ids"])

    def test_distinct_sources_still_need_known_origins_and_explicit_independence_review(self):
        sources = [self.source(), self.source("demo:tool")]
        proposition = self.proposition()
        proposition.update(kind="empirical", quantifier="bounded")
        identity, _ = self.create_claim(sources[0], proposition=proposition)
        evidence = self.replace(identity, [self.evidence_spec(source, source_kind="direct_observation", origin_status="unknown")
                                           for source in sources])
        search = self.search(identity)
        request = self.review_request(identity, evidence, search, profile="independent_observations", source_independence="independent",
            independence_pairs=[{"evidence_ids": [item["id"] for item in evidence], "status": "independent", "reason": "Demo unresolved roots"}])
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        evidence = self.replace(identity, [self.evidence_spec(source, source_kind="direct_observation") for source in sources])
        search = self.search(identity)
        request = self.review_request(identity, evidence, search, profile="independent_observations")
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/search-observations", body=self.claim_command(identity,
            dependencies=[{"pair": {"evidence_ids": [item["id"] for item in evidence], "status": "independent",
                "reason": "Fictional original records describe separately collected observations"},
                "method": "original_provenance_inspection", "verification_refs": sources}]))
        self.review(identity, evidence, search, profile="independent_observations", source_independence="independent",
            independence_pairs=[{"evidence_ids": [item["id"] for item in evidence], "status": "independent", "reason": "Demo reviewed separate observations"}])
        self.assertEqual(self.claim(identity)["data"]["knowledge_state"], "established")

    def test_partial_span_is_bound_to_full_source_snapshot_and_manual_review(self):
        identity, source = self.create_claim()
        evidence = self.replace(identity, [self.evidence_spec(source)])
        stored = self.service.get(self.principal, evidence[0]["id"])["data"]
        text = self.events[0]["content"]
        self.assertEqual(stored["snapshot"]["excerpt"], text[13:37])
        self.assertEqual(stored["snapshot"]["source_digest"], digest(text))
        self.assertEqual(stored["snapshot"]["source_actor_id"], "demo:narrator")
        self.assertEqual(stored["review_mode"], "manual_attestation")
        for start, end in ((-1, 4), (4, 4), (10, 5), (0, len(text) + 1)):
            with self.subTest(span=(start, end)):
                request = self.claim_command(identity, evidence=[self.evidence_spec(source, span_start=start, span_end=end)])
                self.call("PUT", KNOWLEDGE + f"/claims/{identity}/evidence", body=request, status=422)

    def test_review_authentication_and_server_owned_context_are_enforced(self):
        identity, source, evidence, search = self.prepared()
        self.call("GET", KNOWLEDGE + f"/claims/{identity}", token=None, status=401)
        self.call("GET", KNOWLEDGE + f"/claims/{identity}", token="demo-other-owner", status=404)
        requests = [
            ("PUT", "evidence", self.claim_command(identity, evidence=[self.evidence_spec(source)])),
            ("POST", "searches", self.claim_command(identity, status="planned", search_scope="Demo", executor_version="demo")),
            ("POST", "reviews", self.review_request(identity, evidence, search)),
        ]
        for method, path, request in requests:
            with self.subTest(route=path):
                self.call(method, KNOWLEDGE + f"/claims/{identity}/{path}", body=request, token="demo-writer", status=403)
        request = self.review_request(identity, evidence, search)
        request["adoption_context"] = {"review_authorized": True, "path_eligible": True, "references_resolvable": True}
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        request = self.review_request(identity, evidence, search)
        request["evidence"] = []
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        source_request = self.command(scope={"conversation_id": "demo:chat"}, event_ref={"id": "demo:user", "version": 1})
        self.call("POST", KNOWLEDGE + "/sources", body=source_request, token="demo-reader", status=403)

    def test_all_reads_are_historical_and_no_context_injection_occurs(self):
        identity, _, evidence, search = self.prepared()
        self.review(identity, evidence, search)
        for path in (KNOWLEDGE + f"/claims/{identity}", PREFIX + f"/records/{identity}"):
            with self.subTest(path=path):
                result = self.call("GET", path)
                self.assertEqual(result["interpretation"], "historical_snapshot")
                self.assertEqual(result["current_applicability"], "not_evaluated")
        for path in (KNOWLEDGE + "/claims?conversation_id=demo:chat",
                     PREFIX + "/records?kind=knowledge_claim&conversation_id=demo:chat"):
            records = self.call("GET", path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["current_applicability"], "not_evaluated")
        run = self.service.build_run(self.principal, {"conversation_id": "demo:chat", "path_revision": "r1",
            "query": "demo beacon", "idempotency_key": "demo:context-run"})
        self.assertEqual(run["data"]["compiled_memory_ids"], [])
        self.assertNotIn(identity, str(run["data"]["packet"]))
        status = self.call("GET", PREFIX + "/status")["knowledge"]
        self.assertFalse(status["context_injection"])
        self.assertTrue(status["countersearch_execution"])
        self.assertFalse(status["external_countersearch"])

    def test_manual_search_preserves_failure_and_cannot_certify_no_counterevidence(self):
        identity, source = self.create_claim()
        evidence = self.replace(identity, [self.evidence_spec(source)])
        request = self.claim_command(identity, status="blocked", search_scope="Demo offline corpus",
            planned_queries=["Check opposing observations"], executor_version="demo:manual-v1", stop_reason="Demo corpus unavailable")
        result = self.call("POST", KNOWLEDGE + f"/claims/{identity}/searches", body=request)
        search = self.ack_ref(result, "knowledge_search")
        stored = self.service.get(self.principal, search["id"])["data"]
        self.assertEqual(stored["execution_mode"], "manual_attestation")
        self.assertEqual(stored["snapshot"]["status"], "blocked")
        self.assertIsNone(stored["snapshot"]["completed_at"])
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews",
                  body=self.review_request(identity, evidence, search), status=422)

    def test_stale_evidence_set_cannot_reuse_previous_search_attestation(self):
        identity, source, evidence, search = self.prepared()
        new_evidence = self.replace(identity, [self.evidence_spec(source)])
        request = self.review_request(identity, new_evidence, search)
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=409)
        new_search = self.search(identity)
        request = self.review_request(identity, evidence, new_search)
        self.call("POST", KNOWLEDGE + f"/claims/{identity}/reviews", body=request, status=422)
        self.review(identity, new_evidence, new_search)


if __name__ == "__main__":
    unittest.main()

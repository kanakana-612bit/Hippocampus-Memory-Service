"""Fictional episode generation and worker tests; no production endpoints or data."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import unittest
import uuid
from unittest.mock import patch

from v02.episode_contracts import EpisodeProposal, GeneratorMetadata, raw_text_digest
from v02.episode_generation import (EpisodeGenerator, GenerationError, ModelConfig,
    TransportError, json_request, strict_json, validate_grounding)
from v02.episode_worker import EpisodeWorker, daily_due_day


DEMO_TEXT = "架空の灯台計画では青色を採用します。"
DEMO_SECRET = "fictional-provider-key"


def bundle():
    return {"sources": [{"event_ref": {"id": "demo:user", "version": 1},
        "source_digest": raw_text_digest(DEMO_TEXT), "raw_text": DEMO_TEXT,
        "speaker": "user", "event_time": "2026-09-23T00:00:00Z"}],
        "chunks": [{"ref": {"id": "demo:chunk", "version": 1},
                    "source_event_ids": ["demo:user"]}], "existing_episodes": []}


def span():
    return {"event_ref": {"id": "demo:user", "version": 1},
        "source_digest": raw_text_digest(DEMO_TEXT), "start": 0,
        "end": len(DEMO_TEXT), "quote": DEMO_TEXT}


def proposal():
    return {"summaries": [{"chunk_ref": {"id": "demo:chunk", "version": 1},
        "summary": "架空の灯台の色に関する発言。", "topics": ["灯台"],
        "source_spans": [span()], "decision_candidates": [
            {"text": "ユーザーは青色を採用すると述べた。", "source_spans": [span()]}]}],
        "episodes": [], "structured_candidates": []}


def completion(value=None, *, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason,
        "message": {"role": "assistant", "content": json.dumps(value or proposal(), ensure_ascii=False)}}]}


class DemoProvider(BaseHTTPRequestHandler):
    response = completion()
    status = 200
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((self.path, self.headers.get("Authorization"), request))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        if type(self).status == 302:
            self.send_header("Location", "/credential-must-not-follow")
        self.end_headers()
        response = type(self).response(request) if callable(type(self).response) else type(self).response
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode())


class EpisodeGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port) + "/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        DemoProvider.response, DemoProvider.status, DemoProvider.received = completion(), 200, []
        self.generator = EpisodeGenerator(ModelConfig(self.url, "fictional-model", api_key=DEMO_SECRET))

    def test_local_provider_request_and_grounded_schema(self):
        before = deepcopy(bundle())
        result = self.generator.generate("provisional", before)
        self.assertEqual(result.summaries[0].source_spans[0].quote, DEMO_TEXT)
        self.assertEqual(before, bundle())
        path, authorization, request = DemoProvider.received[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(authorization, "Bearer " + DEMO_SECRET)
        self.assertFalse(request["stream"])
        self.assertEqual(request["response_format"], {"type": "json_object"})
        supplied = json.loads(request["messages"][1]["content"])
        self.assertEqual(supplied["input_bundle"], bundle())
        self.assertIn("untrusted data", request["messages"][0]["content"])
        self.assertNotIn(DEMO_SECRET, repr(self.generator.config))
        self.assertEqual(self.generator.metadata().model_id, "fictional-model")

    def test_provider_http_error_and_redirect_are_sanitized_and_not_followed(self):
        for status in (400, 302):
            with self.subTest(status=status):
                DemoProvider.status = status
                DemoProvider.response = {"error": DEMO_TEXT + DEMO_SECRET}
                with self.assertRaises(TransportError) as caught:
                    self.generator.generate("provisional", bundle())
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn(DEMO_SECRET, str(caught.exception))
                self.assertNotIn(DEMO_TEXT, str(caught.exception))
        self.assertEqual(len(DemoProvider.received), 2)

    def test_truncated_refused_and_tool_responses_never_return_proposals(self):
        responses = [completion(finish_reason="length"), completion(finish_reason="tool_calls"),
            {"choices": []}, {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(proposal()), "tool_calls": [{"function": "demo"}]}}]}]
        for response in responses:
            with self.subTest(response_type=type(response).__name__), patch(
                    "v02.episode_generation.json_request", return_value=response):
                with self.assertRaisesRegex(GenerationError, "invalid_or_incomplete"):
                    self.generator.generate("provisional", bundle())

    def test_strict_json_rejects_duplicate_members_nonfinite_and_fences(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}', '```json\n{}\n```'):
            with self.assertRaises(ValueError):
                strict_json(value)

    def test_unknown_fields_or_direct_adoption_are_rejected_without_echo(self):
        for field, value in (("confidence", DEMO_SECRET), ("generalization_weight", 1.0)):
            bad = proposal()
            bad[field] = value
            with patch("v02.episode_generation.json_request", return_value=completion(bad)):
                with self.assertRaises(GenerationError) as caught:
                    self.generator.generate("provisional", bundle())
                self.assertNotIn(DEMO_SECRET, str(caught.exception))
                self.assertNotIn(DEMO_TEXT, str(caught.exception))

    def test_forged_span_and_stale_digest_are_rejected_even_when_schema_valid(self):
        for mutation in ("quote", "digest", "version", "id"):
            bad = proposal()
            item = bad["summaries"][0]["source_spans"][0]
            if mutation == "quote":
                item["quote"] = "別" * len(DEMO_TEXT)
            elif mutation == "digest":
                item["source_digest"] = "0" * 64
            else:
                item["event_ref"][mutation] = 2 if mutation == "version" else "invented:source"
            with self.subTest(mutation=mutation), patch(
                    "v02.episode_generation.json_request", return_value=completion(bad)):
                with self.assertRaisesRegex(GenerationError, "source_span_mismatch"):
                    self.generator.generate("provisional", bundle())

    def test_corrupted_input_fails_before_provider_call(self):
        bad = bundle()
        bad["sources"][0]["raw_text"] += "改変"
        with patch("v02.episode_generation.json_request") as called:
            with self.assertRaisesRegex(GenerationError, "source_manifest_invalid"):
                self.generator.generate("daily", bad)
            called.assert_not_called()

    def test_nested_structured_span_uses_exact_unicode_codepoints(self):
        value = proposal()
        text = "架空🛰計画"
        input_bundle = bundle()
        input_bundle["sources"][0].update(raw_text=text, source_digest=raw_text_digest(text))
        value["summaries"] = []
        value["structured_candidates"] = [{"kind": "user_intent", "subject": "demo:user",
            "topic": "架空計画", "text": "架空計画を検討。", "source_spans": [{
                "event_ref": {"id": "demo:user", "version": 1},
                "source_digest": raw_text_digest(text), "start": 2, "end": 3, "quote": "🛰"}]}]
        result = EpisodeProposal.model_validate(value)
        validate_grounding(result, input_bundle)
        self.assertEqual(result.structured_candidates[0].status, "candidate")

    def test_response_processing_limit_does_not_truncate_into_valid_content(self):
        DemoProvider.response = {"fictional_padding": "x" * 2048}
        with self.assertRaisesRegex(TransportError, "response_processing_limit"):
            json_request(self.url + "/chat/completions", payload={}, response_byte_limit=1024)

    def test_worker_canonical_and_provider_roundtrip_preserves_sources_and_candidate_status(self):
        from fastapi.testclient import TestClient
        from v02.api import create_app
        from v02.service import Service
        root = Path(__file__).resolve().parents[1] / "tmp-tests"
        root.mkdir(exist_ok=True)
        folder = root / ("episode-worker-demo-" + uuid.uuid4().hex)
        folder.mkdir()
        def clean():
            target = folder.resolve()
            self.assertEqual(target.parent, root.resolve())
            self.assertTrue(target.name.startswith("episode-worker-demo-"))
            shutil.rmtree(target)
        self.addCleanup(clean)
        environment = patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        clock = patch("v02.episode_service.now", return_value="2026-09-22T18:00:00Z")
        clock.start()
        self.addCleanup(clock.stop)
        service = Service(folder / "fictional.db")
        principal, token = "demo:worker-owner", "demo:worker-token"
        service.sync(principal, {"conversation_id": "demo:conversation", "revision_key": "demo:r1",
            "expected_revision": None, "auto_capture": False,
            "active_event_ids": ["demo:user", "demo:reply"], "messages": [
                {"id": "demo:user", "role": "user", "content": DEMO_TEXT,
                 "event_time": "2026-09-22T00:00:00Z"},
                {"id": "demo:reply", "role": "assistant", "content": "架空の計画として記録しました。",
                 "event_time": "2026-09-22T00:01:00Z"}]})
        original = deepcopy(service.list_records(principal, "event"))
        client = TestClient(create_app(service, [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal_id": principal, "host_id": "demo:worker", "permissions": ["read", "write"]}]))
        self.addCleanup(client.close)

        class ASGIClient:
            def request(self, method, route, payload=None):
                response = client.request(method, "/api/v0.2" + route,
                    headers={"Authorization": "Bearer " + token}, json=payload)
                if response.status_code != 200:
                    raise TransportError("http_failure", status=response.status_code)
                return response.json()

        def generated(request):
            payload = json.loads(request["messages"][1]["content"])
            supplied = payload["input_bundle"]
            spans = {s["event_ref"]["id"]: {"event_ref": s["event_ref"],
                "source_digest": s["source_digest"], "start": 0, "end": len(s["raw_text"]),
                "quote": s["raw_text"]} for s in supplied["sources"]}
            if payload["operation"] == "provisional":
                value = {"summaries": [{"chunk_ref": chunk["ref"], "summary": "架空の灯台の色を検討した。",
                    "source_spans": [spans[i] for i in chunk["source_event_ids"]],
                    "decision_candidates": [{"text": "ユーザーは架空の灯台に青色を採用すると述べた。",
                        "source_spans": [spans["demo:user"]]}]} for chunk in supplied["chunks"]]}
            else:
                value = {"episodes": [{"local_id": "local", "level": "local", "title": "架空の色選び",
                    "summary": "架空の灯台に青色を採用すると述べた。", "source_spans": list(spans.values())},
                    {"local_id": "theme", "level": "theme", "title": "架空の灯台計画",
                     "summary": "架空の灯台計画の色についての記録。", "child_local_ids": ["local"]}]}
            return completion(value)
        DemoProvider.response = generated
        worker = EpisodeWorker(ASGIClient(), self.generator, {"conversation_id": "demo:conversation"})
        result = worker.run_once(at=datetime(2026, 9, 22, 18, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["results"][0]["structured_candidates"], 1)
        self.assertEqual(result["results"][1]["episodes"], 2)
        self.assertEqual(service.list_records(principal, "event"), original)
        self.assertEqual(service.list_records(principal, "knowledge_claim"), [])
        candidates = service.list_records(principal, "structured_candidate")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["data"]["adoption"], "candidate")
        self.assertEqual(len(DemoProvider.received), 2)
        again = worker.run_once(at=datetime(2026, 9, 22, 18, tzinfo=timezone.utc))
        self.assertEqual([x["status"] for x in again["results"]], ["no_work", "no_work"])
        self.assertEqual(len(DemoProvider.received), 2)


class FakeGenerator:
    def __init__(self):
        self.calls = []

    def generate(self, operation, input_bundle):
        self.calls.append((operation, deepcopy(input_bundle)))
        return EpisodeProposal.model_validate(proposal())

    def metadata(self):
        return GeneratorMetadata(model_id="fictional-model", prompt_version="demo.v1", algorithm_version="demo.v1")


class FakeCanonical:
    def __init__(self):
        self.calls = []
        self.no_work = False
        self.commit_failures = []

    def request(self, method, route, body=None):
        self.calls.append((method, route, deepcopy(body)))
        if route.startswith("/dialogue/path?"):
            return {"data": {"revision_key": "demo-path-current"}}
        if route.endswith("/seal"):
            return {"status": "sealed"}
        if route.endswith("/prepare"):
            if self.no_work:
                return {"status": "no_work", "job_id": None}
            return {"job_id": "demo:job", "manifest_digest": "b" * 64,
                "operation": body["operation"], "input_bundle": bundle()}
        if route.endswith("/commit"):
            if self.commit_failures:
                raise self.commit_failures.pop(0)
            return {"job_id": "demo:job", "summary_refs": [{"id": "demo:summary", "version": 1}],
                "episode_refs": [], "candidate_refs": [{"id": "demo:candidate", "version": 1}],
                "raw_changed": False, "knowledge_promoted": False}
        raise AssertionError("Unexpected route")


class EpisodeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.client, self.generator = FakeCanonical(), FakeGenerator()
        self.worker = EpisodeWorker(self.client, self.generator,
                                    {"conversation_id": "demo:conversation"})

    def test_jst_three_am_and_month_year_boundaries_without_zone_database(self):
        checks = {"2026-09-22T17:59:59+00:00": "2026-09-21",
                  "2026-09-22T18:00:00+00:00": "2026-09-22",
                  "2026-12-31T18:00:00+00:00": "2026-12-31",
                  "2026-03-01T03:00:00+09:00": "2026-02-28"}
        for when, expected in checks.items():
            self.assertEqual(daily_due_day(datetime.fromisoformat(when)), expected)
        with self.assertRaises(ValueError):
            daily_due_day(datetime(2026, 9, 23, 3))

    def test_once_seals_then_provisional_then_daily_with_fresh_path(self):
        result = self.worker.run_once(at=datetime(2026, 9, 22, 18, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["semantic_correctness"], "not_attested")
        self.assertEqual([x[0] for x in self.generator.calls], ["provisional", "daily"])
        preparations = [body for _, route, body in self.client.calls if route.endswith("/prepare")]
        self.assertEqual(preparations[-1]["day"], "2026-09-22")
        self.assertEqual(preparations[0]["path_revision"], "demo-path-current")
        self.assertEqual(sum(route.startswith("/dialogue/path?") for _, route, _ in self.client.calls), 3)
        self.assertNotIn(DEMO_TEXT, json.dumps(result, ensure_ascii=False))

    def test_no_work_does_not_call_provider_or_skip_future_backlog(self):
        self.client.no_work = True
        self.worker.run_once()
        self.assertEqual(self.generator.calls, [])
        self.client.no_work = False
        self.worker.run_once()
        self.assertEqual(len(self.generator.calls), 2)

    def test_uncertain_commit_retries_identical_body_without_generating_twice(self):
        self.client.commit_failures = [TransportError("connection_failure")]
        self.worker.process("provisional")
        commits = [body for _, route, body in self.client.calls if route.endswith("/commit")]
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0], commits[1])
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(commits[0]["manifest_digest"], "b" * 64)
        self.assertEqual(commits[0]["generator"]["model_id"], "fictional-model")

    def test_path_conflict_does_not_retry_stale_proposal_or_continue_daily(self):
        self.client.commit_failures = [TransportError("http_failure", status=409)]
        with self.assertRaises(TransportError):
            self.worker.run_once()
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(sum(route.endswith("/commit") for _, route, _ in self.client.calls), 1)

    def test_incomplete_commit_acknowledgement_is_not_reported_as_success(self):
        original = self.client.request
        def changed(method, route, body=None):
            result = original(method, route, body)
            if route.endswith("/commit"):
                result["candidate_refs"] = []
            return result
        self.client.request = changed
        with self.assertRaisesRegex(GenerationError, "acknowledgement_invalid"):
            self.worker.process("provisional")

    def test_regeneration_uses_explicit_versioned_sources_and_does_not_seal(self):
        self.worker.run_once(operation="regenerate", source_refs=[{"id": "demo:episode", "version": 2}])
        self.assertFalse(any(route.endswith("/seal") for _, route, _ in self.client.calls))
        body = next(body for _, route, body in self.client.calls if route.endswith("/prepare"))
        self.assertEqual(body["source_refs"], [{"id": "demo:episode", "version": 2}])


if __name__ == "__main__":
    unittest.main()

"""All data and providers in this module are fictional; no network is used."""
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import unittest
import uuid

from v02.evaluation import (ALLOWED_OUTPUT_ROOTS, EvaluationRunner, OpenAICompatibleProvider,
                            ProviderFailure, Rubric, grade)


FIXTURE = Path(__file__).parent / "fixtures" / "v02-evaluation-mainline.json"


class FakeProvider:
    model = "fictional-model"
    base_url = "http://fixture.invalid/v1"
    stream = False

    def __init__(self, failure=None, invalid_answer=False, empty_answer=False):
        self.calls = []
        self.failure = failure
        self.invalid_answer = invalid_answer
        self.empty_answer = empty_answer

    def discover(self):
        return {"status": "available", "requested_model_available": True, "model_ids": [self.model]}

    def generate(self, payload):
        self.calls.append(payload)
        if self.failure:
            raise ProviderFailure(self.failure)
        if len(payload["messages"]) == 2:
            answer = {key: "unknown" for key in ("answer", "actor", "condition", "scope", "certainty")}
        else:
            answer = {"answer": "blue", "actor": "Mira", "condition": "rainy_morning", "scope": "single_event", "certainty": "reported"}
        return {"content": "" if self.empty_answer else "not json" if self.invalid_answer else json.dumps(answer),
                "reported_model_id": self.model, "finish_reason": "length" if self.empty_answer else "stop", "prompt_tokens": 90,
                "completion_tokens": 30, "total_latency_ms": 100.0, "first_token_ms": None}


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return io.BytesIO(self.payload)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.root = ALLOWED_OUTPUT_ROOTS[0].resolve()
        self.root.mkdir(exist_ok=True)
        self.directory = (self.root / ("unit-eval-" + uuid.uuid4().hex)).resolve()
        self.directory.mkdir()
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self):
        if self.directory.parent != self.root or not self.directory.name.startswith("unit-eval-"):
            raise RuntimeError("Refusing cleanup outside the evaluation fixture root")
        shutil.rmtree(self.directory)

    def runner(self, fixture=None, provider=None, name="evaluation"):
        return EvaluationRunner(fixture or self.fixture, self.directory / name, provider=provider)

    def results(self, name="evaluation"):
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(
            (self.directory / name).glob("*/attempt-*/result.json"))]

    def test_dry_run_separates_selection_compilation_finalization_without_model_call(self):
        provider = FakeProvider()
        summary = self.runner(provider=provider).run()
        self.assertEqual(summary["status"], "prepared", self.results())
        self.assertIsNone(summary["quality_passed"])
        self.assertEqual(provider.calls, [])
        results = self.results()
        self.assertEqual(len(results), 4)
        for result in results:
            self.assertTrue(result["selection"])
            self.assertEqual(result["compiled"]["compiled_memory_ids"], ["eval:memory"])
            self.assertTrue(result["permanent_state_unchanged"])
            self.assertTrue(result["integrity_valid"])
            self.assertEqual(result["data_origin"], "new_fictional_fixture")
        baseline = next(row for row in results if row["case_id"] == "baseline_no_injection")
        injected = next(row for row in results if row["case_id"] == "episodic_context")
        self.assertEqual(baseline["host_finalized"]["status"], "not_present")
        self.assertEqual(baseline["host_finalized"]["memory_ids"], [])
        self.assertEqual(injected["host_finalized"]["status"], "retained")
        self.assertEqual(injected["host_finalized"]["memory_ids"], ["eval:memory"])
        self.assertNotEqual(baseline["wire_request_digest"], injected["wire_request_digest"])
        run_path = self.directory / "evaluation" / "episodic_context-0" / "attempt-0001" / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        for entry in run["data"]["packet"]["derived_memories"]:
            self.assertTrue(entry["provenance"]["created_at"].startswith("2026-09-01"))

    def test_execute_records_metrics_completion_and_resume_does_not_call_again(self):
        provider = FakeProvider()
        runner = self.runner(provider=provider)
        summary = runner.run(execute=True)
        self.assertEqual(summary["status"], "evaluated", self.results())
        self.assertTrue(summary["quality_passed"], self.results())
        self.assertEqual(len(provider.calls), 4)
        for row in self.results():
            self.assertEqual(row["metrics"]["prompt_tokens"], 90)
            self.assertEqual(row["metrics"]["completion_tokens"], 30)
            self.assertIsNone(row["metrics"]["first_token_ms"])
        self.assertTrue(runner.run(execute=True, resume=True)["quality_passed"])
        self.assertEqual(len(provider.calls), 4)

    def test_provider_failure_unknown_retry_uses_a_new_isolated_attempt(self):
        fixture = deepcopy(self.fixture)
        fixture["cases"] = fixture["cases"][:1]
        provider = FakeProvider("provider_TimeoutError")
        runner = self.runner(fixture, provider)
        failed = runner.run(execute=True)
        self.assertEqual(failed["status"], "blocked", self.results())
        self.assertIsNone(failed["quality_passed"])
        self.assertIsNone(failed["results"][0]["quality"]["passed"])
        provider.failure = None
        resumed = runner.run(execute=True, resume=True)
        self.assertTrue(resumed["quality_passed"], self.results())
        self.assertEqual(len(self.results()), 2)
        self.assertEqual(len(provider.calls), 2)

    def test_invalid_answer_is_unscored_and_provider_absence_is_not_a_pass(self):
        fixture = deepcopy(self.fixture)
        fixture["cases"] = fixture["cases"][:1]
        result = self.runner(fixture, FakeProvider(invalid_answer=True)).run(execute=True)
        self.assertIsNone(result["quality_passed"], self.results())
        self.assertEqual(result["results"][0]["quality"]["status"], "invalid_answer_json")
        with self.assertRaises(ValueError):
            self.runner(name="without-provider").run(execute=True)

    def test_changed_manifest_and_live_directory_are_rejected(self):
        runner = self.runner()
        runner.run()
        altered = deepcopy(self.fixture)
        altered["system_prompt"] += " Changed template."
        with self.assertRaises(ValueError):
            self.runner(altered).run(resume=True)
        with self.assertRaises(ValueError):
            EvaluationRunner(self.fixture, FIXTURE.parents[2] / "data" / "evaluation")

    def test_empty_truncated_answer_preserves_usage_without_fabricating_response_event(self):
        fixture = deepcopy(self.fixture)
        fixture["cases"] = fixture["cases"][:1]
        result = self.runner(fixture, FakeProvider(empty_answer=True)).run(execute=True)
        self.assertEqual(result["status"], "blocked", self.results())
        row = self.results()[0]
        self.assertEqual(row["status"], "provider_incomplete")
        self.assertEqual(row["quality"]["status"], "incomplete_or_unknown_finish")
        self.assertEqual(row["metrics"]["prompt_tokens"], 90)
        self.assertIsNone(result["quality_passed"])
        self.assertTrue(row["integrity_valid"])
        self.assertTrue(row["permanent_state_unchanged"])

    def test_dictionary_fixture_uses_real_review_gate_and_separate_shadow_observation(self):
        fixture = json.loads(FIXTURE.with_name("v02-evaluation-dictionary-isolated.json").read_text(encoding="utf-8"))
        summary = self.runner(fixture).run()
        self.assertEqual(summary["status"], "prepared", self.results())
        rows = {row["case_id"]: row for row in self.results()}
        self.assertEqual(len(rows), 7)
        for key in ("dictionary_disabled", "dictionary_shadow"):
            run_file = self.directory / "evaluation" / (key + "-0") / "attempt-0001" / "run.json"
            packet = json.loads(run_file.read_text(encoding="utf-8"))["data"]["packet"]
            self.assertEqual(len(packet["source_evidence"]), 6)
            self.assertNotIn("eval:source", json.dumps(packet))
            self.assertNotIn("eval:narrator", json.dumps(packet))
        shadow = rows["dictionary_shadow"]
        self.assertEqual(len(shadow["knowledge_selection"]), 1)
        self.assertEqual(shadow["compiled"]["compiled_claims"], [])
        self.assertTrue(rows["dictionary_adopted"]["compiled"]["compiled_claims"])
        for key in ("dictionary_provisional", "dictionary_withdrawn", "dictionary_independence_missing", "dictionary_condition_unknown"):
            self.assertEqual([item for item in rows[key]["compiled"]["compiled_claims"] if item["section"] == "dictionary_claims"], [])
            self.assertEqual(rows[key]["knowledge_selection"][0]["section"], "knowledge_caveats")

    def test_fixture_packet_expectation_failure_stops_before_provider(self):
        fixture = deepcopy(self.fixture)
        fixture["cases"] = fixture["cases"][:1]
        fixture["cases"][0]["packet_expectations"] = {"dictionary_claims": 2}
        provider = FakeProvider()
        summary = self.runner(fixture, provider).run(execute=True)
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.results()[0]["error_code"], "fixture_packet_expectation_failed")

    def test_discovery_failure_is_persisted_unknown_without_generation(self):
        provider = FakeProvider()
        def failure():
            raise ProviderFailure("discovery_URLError_TimeoutError")
        provider.discover = failure
        summary = self.runner(provider=provider).run(execute=True)
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["stage"], "discovery")
        self.assertIsNone(summary["quality_passed"])
        self.assertEqual(provider.calls, [])
        saved = json.loads((self.directory / "evaluation" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["discovery"]["error_code"], "discovery_URLError_TimeoutError")

    def test_runner_rejects_a_provider_with_a_different_requested_model(self):
        with self.assertRaisesRegex(ValueError, "model must match"):
            EvaluationRunner(self.fixture, self.directory / "mismatch", provider=FakeProvider(), model="another-model")

    def test_interrupted_attempt_is_unknown_and_resume_uses_another_database(self):
        fixture = deepcopy(self.fixture)
        fixture["cases"] = fixture["cases"][:1]
        provider = FakeProvider()
        runner = self.runner(fixture, provider)
        runner.run()
        old = next((self.directory / "evaluation").glob("*/attempt-*/result.json"))
        value = json.loads(old.read_text(encoding="utf-8"))
        value["status"] = "in_progress"
        old.write_text(json.dumps(value), encoding="utf-8")
        summary = runner.run(execute=True, resume=True)
        self.assertTrue(summary["quality_passed"])
        self.assertEqual(json.loads(old.read_text(encoding="utf-8"))["status"], "interrupted_unknown")
        self.assertEqual(len(list((self.directory / "evaluation").glob("*/attempt-*/fixture.db"))), 2)
        self.assertEqual(len(provider.calls), 1)

    def test_unavailable_measurements_remain_unknown_separate_from_answer_quality(self):
        fixture = deepcopy(self.fixture)
        fixture["cases"] = fixture["cases"][:1]
        provider = FakeProvider()
        generate = provider.generate
        def missing_metrics(payload):
            answer = generate(payload)
            for key in ("prompt_tokens", "completion_tokens", "total_latency_ms", "first_token_ms"):
                answer[key] = None
            return answer
        provider.generate = missing_metrics
        result = self.runner(fixture, provider).run(execute=True)
        self.assertTrue(result["quality_passed"])
        self.assertEqual(result["measurement_status"], "unknown")
        self.assertEqual(result["release_readiness"], "not_established_by_this_run")
        for key in ("prompt_tokens", "completion_tokens", "total_latency_ms", "first_token_ms"):
            self.assertIsNone(result["results"][0]["metrics"][key])


class ProviderAndRubricTests(unittest.TestCase):
    def test_fixed_grading_has_no_self_judging_or_fabricated_scores(self):
        rubric = Rubric(expected={"actor": ("Mira",), "scope": ("single_event",)})
        self.assertTrue(grade('{"actor":"Mira","scope":"single_event"}', rubric, finish_reason="stop")["passed"])
        wrong = grade('{"actor":"Sol","scope":"general"}', rubric, finish_reason="stop")
        self.assertFalse(wrong["passed"])
        self.assertEqual(wrong["axes"], {"actor": False, "scope": False})
        for content, finish in (("broken", "stop"), ('{"actor":"Mira"}', "stop"), ("{}", "length")):
            result = grade(content, rubric, finish_reason=finish)
            self.assertIsNone(result["passed"])
            self.assertIsNone(result["axes"])
        correct = '{"actor":"Mira","scope":"single_event"}'
        for content, finish in ((correct, "length"), (correct, None), ("```json\n" + correct + "\n```", "stop")):
            self.assertIsNone(grade(content, rubric, finish_reason=finish)["passed"])

    def test_nonstream_usage_unknown_and_model_identifier_exact(self):
        body = {"model": "fixture-model", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        provider = OpenAICompatibleProvider("http://fixture.invalid/v1", "fixture-model", opener=FakeOpener(body))
        result = provider.generate({"model": "fixture-model"})
        self.assertIsNone(result["prompt_tokens"])
        self.assertIsNone(result["completion_tokens"])
        self.assertIsNone(result["first_token_ms"])
        for bad in ("other-model", None):
            changed = deepcopy(body)
            changed["model"] = bad
            provider.opener = FakeOpener(changed)
            with self.assertRaisesRegex(ProviderFailure, "provider_model_mismatch"):
                provider.generate({})

    def test_stream_first_content_and_usage_and_truncation(self):
        stream = '\n'.join([
            'data: {"model":"fixture-model","choices":[{"delta":{"content":"{}"},"finish_reason":null}]}',
            'data: {"model":"fixture-model","choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"model":"fixture-model","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2}}',
            'data: [DONE]', ''])
        provider = OpenAICompatibleProvider("http://fixture.invalid/v1", "fixture-model", stream=True, opener=FakeOpener(stream))
        result = provider.generate({})
        self.assertEqual(result["content"], "{}")
        self.assertIsNotNone(result["first_content_ms"])
        self.assertIsNone(result["first_token_ms"])
        self.assertEqual(result["prompt_tokens"], 12)
        provider.opener = FakeOpener(stream.replace('data: [DONE]', ''))
        with self.assertRaisesRegex(ProviderFailure, "incomplete_provider_stream"):
            provider.generate({})

    def test_malformed_provider_and_credential_echo_fail_without_exposing_body(self):
        for body in ("not json", {"choices": []}, {"choices": [{"message": {"content": "private-fixture-key"}}]}):
            provider = OpenAICompatibleProvider("http://fixture.invalid/v1", "fixture-model", api_key="private-fixture-key", opener=FakeOpener(body))
            with self.assertRaises(ProviderFailure) as caught:
                provider.generate({})
            self.assertNotIn("private-fixture-key", str(caught.exception))
            self.assertNotIn("not json", str(caught.exception))

    def test_discovery_is_readonly_and_excludes_arbitrary_provider_fields(self):
        opener = FakeOpener({"data": [{"id": "fixture-model", "context_length": 8192, "internal_path": "do-not-record"}]})
        provider = OpenAICompatibleProvider("http://fixture.invalid/v1", "fixture-model", opener=opener)
        result = provider.discover()
        self.assertTrue(result["requested_model_available"])
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertIsNone(opener.requests[0].data)
        self.assertNotIn("do-not-record", json.dumps(result))

    def test_discovery_capabilities_cannot_echo_credentials(self):
        opener = FakeOpener({"data": [{"id": "fixture-model", "context_length": "fictional-secret-key"}]})
        provider = OpenAICompatibleProvider("http://fixture.invalid/v1", "fixture-model", api_key="fictional-secret-key", opener=opener)
        result = provider.discover()
        self.assertNotIn("fictional-secret-key", json.dumps(result))
        self.assertNotIn("context_length", result["model_capabilities"][0])

    def test_stream_rejects_mixed_or_missing_content_chunk_model(self):
        for bad_model in ("wrong-model", None):
            first = {"model": bad_model, "choices": [{"delta": {"content": "bad"}, "finish_reason": None}]}
            last = {"model": "fixture-model", "choices": [{"delta": {}, "finish_reason": "stop"}]}
            stream = "data: " + json.dumps(first) + "\ndata: " + json.dumps(last) + "\ndata: [DONE]\n"
            provider = OpenAICompatibleProvider("http://fixture.invalid/v1", "fixture-model", stream=True, opener=FakeOpener(stream))
            with self.assertRaisesRegex(ProviderFailure, "provider_model_mismatch"):
                provider.generate({})


if __name__ == "__main__":
    unittest.main()

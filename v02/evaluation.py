"""K5 fixture-only evaluation host. No production DB, registry, or chat import."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Literal
import urllib.error
import urllib.parse
import urllib.request
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import Field, model_validator

from .api import create_app
from . import contracts
from .contracts import Contract, canonical, digest, timestamp
from .host import final_input
from .service import Service


FORMAT = "hippocampus.evaluation.k5.v1"
ROOT = Path(__file__).resolve().parents[1]
ALLOWED_OUTPUT_ROOTS = (ROOT / "tmp-evaluations", ROOT.parent / "outputs" / "hippocampus-evaluations")


class ApiStep(Contract):
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    method: Literal["GET", "POST", "PUT", "PATCH"]
    route: str
    body: dict | None = None
    expected_status: int = Field(default=200, ge=200, le=499)

    @model_validator(mode="after")
    def local_route(self):
        if not self.route.startswith("/") or self.route.startswith("//") or "://" in self.route or ".." in self.route:
            raise ValueError("Fixture steps use relative canonical API routes only")
        return self


class Rubric(Contract):
    mode: Literal["exact_json", "manual"] = "exact_json"
    expected: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_ground_truth(self):
        if self.mode == "exact_json" and (not self.expected or any(not values for values in self.expected.values())):
            raise ValueError("Exact grading requires fixed expected values for each answer field")
        return self


class EvaluationCase(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    question: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    conversation_id: str = "eval:chat"
    path_revision: str = "r1"
    inject_packet: bool = True
    knowledge_mode: Literal["disabled", "shadow", "enabled"] | None = None
    steps: tuple[ApiStep, ...] = ()
    run_options: dict = Field(default_factory=dict)
    packet_expectations: dict[str, int] = Field(default_factory=dict)
    packet_must_not_contain: tuple[str, ...] = ()
    rubric: Rubric


class EvaluationFixture(Contract):
    format: Literal["hippocampus.evaluation.fixture.v1"]
    synthetic: Literal[True]
    data_origin: Literal["new_fictional_fixture"]
    fixture_version: int = Field(default=1, ge=1)
    as_of: str
    template_id: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    steps: tuple[ApiStep, ...] = ()
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        timestamp(self.as_of)
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Repeated case ID")
        for case in self.cases:
            names = [step.name for step in (*self.steps, *case.steps)]
            if len(names) != len(set(names)):
                raise ValueError("Repeated setup step name")
        return self


class ProviderFailure(RuntimeError):
    """A non-sensitive error code; never embed request headers or provider bodies."""


def failure_code(exc, prefix):
    name = type(exc).__name__
    if isinstance(exc, urllib.error.URLError):
        name += "_" + type(exc.reason).__name__
    return prefix + name


def safe_base_url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Explicit HTTP(S) provider URL without credentials/query/fragment required")
    return value.rstrip("/")


def _usage(value):
    if value is None:
        return {"prompt_tokens": None, "completion_tokens": None}
    if not isinstance(value, dict):
        raise ProviderFailure("invalid_provider_usage")
    result = {}
    for key in ("prompt_tokens", "completion_tokens"):
        number = value.get(key)
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 0):
            raise ProviderFailure("invalid_provider_usage")
        result[key] = number
    return result


class OpenAICompatibleProvider:
    """Direct evaluation transport; no implicit fallback, key discovery, or retries."""
    def __init__(self, base_url, model, *, api_key=None, timeout=60, stream=False, opener=None):
        self.base_url = safe_base_url(base_url)
        if not isinstance(model, str) or not model or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Explicit model ID and positive timeout required")
        self.model, self.api_key, self.timeout, self.stream = model, api_key, timeout, stream
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, route, payload=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return urllib.request.Request(self.base_url + route, headers=headers,
            data=canonical(payload).encode("utf-8") if payload is not None else None,
            method="POST" if payload is not None else "GET")

    def discover(self):
        try:
            with self.opener.open(self._request("/models"), timeout=self.timeout) as response:
                payload = json.load(response)
            models = payload.get("data")
            if not isinstance(models, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in models):
                raise ProviderFailure("invalid_model_discovery")
            ids = [item["id"] for item in models]
            self._no_credential(ids)
            self.last_discovery = {"status": "available", "base_url": self.base_url, "model_ids": ids,
                    "requested_model_available": self.model in ids,
                    "model_capabilities": [{"id": item["id"], **{key: item[key] for key in
                        ("context_length", "max_model_len", "max_context_length")
                        if type(item.get(key)) is int and item[key] > 0}} for item in models]}
            self._no_credential(self.last_discovery)
            return self.last_discovery
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(failure_code(exc, "discovery_")) from None

    def _no_credential(self, value):
        if self.api_key and self.api_key in canonical(value):
            raise ProviderFailure("credential_echo_rejected")

    def generate(self, payload):
        started = time.monotonic()
        try:
            with self.opener.open(self._request("/chat/completions", payload), timeout=self.timeout) as response:
                if self.stream:
                    result = self._read_stream(response, started)
                else:
                    body = json.load(response)
                    self._no_credential(body)
                    choices = body.get("choices")
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        raise ProviderFailure("invalid_provider_choices")
                    message = choices[0].get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                        raise ProviderFailure("invalid_provider_content")
                    result = {"content": message["content"], "reported_model_id": body.get("model"),
                              "finish_reason": choices[0].get("finish_reason"), "first_token_ms": None, "first_content_ms": None,
                              **_usage(body.get("usage"))}
            result["total_latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            if result["reported_model_id"] != self.model:
                raise ProviderFailure("provider_model_mismatch_or_missing")
            if result["finish_reason"] is not None and not isinstance(result["finish_reason"], str):
                raise ProviderFailure("invalid_provider_finish")
            self._no_credential(result)
            return result
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(failure_code(exc, "provider_")) from None

    def _read_stream(self, response, started):
        chunks, first, finish, model, done = [], None, None, None, False
        usage = _usage(None)
        for line in response:
            if time.monotonic() - started > self.timeout:
                raise ProviderFailure("provider_total_timeout")
            text = line.decode("utf-8").strip()
            if not text or text.startswith(":"):
                continue
            if not text.startswith("data:"):
                continue
            text = text[5:].strip()
            if text == "[DONE]":
                done = True
                break
            body = json.loads(text)
            self._no_credential(body)
            if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
                raise ProviderFailure("invalid_provider_stream")
            if body.get("model") is not None and body["model"] != self.model:
                raise ProviderFailure("provider_model_mismatch_or_missing")
            if body["choices"] and body.get("model") != self.model:
                raise ProviderFailure("provider_model_mismatch_or_missing")
            model = body.get("model", model)
            if body.get("usage") is not None:
                usage = _usage(body["usage"])
            if not body["choices"]:
                continue
            choice = body["choices"][0]
            content = choice.get("delta", {}).get("content")
            if content is not None and not isinstance(content, str):
                raise ProviderFailure("invalid_provider_stream_content")
            if content:
                if first is None:
                    first = round((time.monotonic() - started) * 1000, 3)
                chunks.append(content)
            finish = choice.get("finish_reason") or finish
        if not done:
            raise ProviderFailure("incomplete_provider_stream")
        return {"content": "".join(chunks), "reported_model_id": model, "finish_reason": finish,
                "first_content_ms": first, "first_token_ms": None, **usage}


def grade(content, rubric, *, finish_reason):
    if rubric.mode == "manual":
        return {"status": "manual_review_required", "passed": None, "axes": None}
    if finish_reason != "stop":
        return {"status": "incomplete_or_unknown_finish", "passed": None, "axes": None}
    try:
        answer = json.loads(content)
    except (ValueError, TypeError):
        return {"status": "invalid_answer_json", "passed": None, "axes": None}
    if not isinstance(answer, dict) or set(answer) != set(rubric.expected) or any(not isinstance(v, str) for v in answer.values()):
        return {"status": "invalid_answer_shape", "passed": None, "axes": None}
    axes = {key: answer[key] in values for key, values in rubric.expected.items()}
    return {"status": "scored_against_fixed_fixture", "passed": all(axes.values()), "axes": axes}


def _write(path, value):
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _lock(path):
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Another evaluation owns this output directory") from None
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _fixture_clock(value):
    # Imported aliases must share the logical time; transport latency uses monotonic time.
    with ExitStack() as stack:
        # Pydantic retains the original Provenance default_factory callable.
        # Its datetime lookup must share the same clock as patched aliases.
        fixed_datetime = stack.enter_context(patch.object(contracts, "datetime", wraps=datetime))
        fixed_datetime.now.return_value = timestamp(value)
        for name, module in tuple(sys.modules.items()):
            if name.startswith("v02.") and callable(getattr(module, "now", None)):
                stack.enter_context(patch.object(module, "now", return_value=value))
        stack.enter_context(patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""}))
        yield


def _resolve(value, bindings):
    def reference(path):
        parts = path.split(".")
        current = bindings[parts[0]]
        for key in parts[1:]:
            current = current[int(key)] if isinstance(current, list) else current[key]
        return deepcopy(current)
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return reference(value["$ref"])
        return {key: _resolve(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, bindings) for item in value]
    if isinstance(value, str):
        return re.sub(r"\{\{([a-zA-Z0-9_.-]+)\}\}", lambda match: str(reference(match.group(1))), value)
    return value


def _permanent_state(service):
    with service.db.connection() as con:
        return digest([tuple(row) for row in con.execute(
            "SELECT id,kind,version,data_json FROM records WHERE kind IN ('memory','edge','knowledge_claim') ORDER BY id")])


def _call(client, method, route, body=None, *, expected_status=200):
    response = client.request(method, "/api/v0.2" + route, json=body)
    if response.status_code != expected_status:
        # Response bodies can contain fixture content: report the stage/status only.
        raise ValueError("fixture_api_" + str(response.status_code) + ":" + method + ":" + route.split("?")[0])
    return response.json()


class EvaluationRunner:
    def __init__(self, fixture, output, *, provider=None, model=None, temperature=0, seed=0,
                 max_tokens=512, repetitions=1, service_factory=Service):
        self.fixture = EvaluationFixture.model_validate(fixture)
        self.output = Path(output).resolve()
        if not any(root.resolve() in self.output.parents for root in ALLOWED_OUTPUT_ROOTS):
            raise ValueError("Output must be a child of hippocampus/tmp-evaluations or outputs/hippocampus-evaluations")
        if repetitions < 1 or max_tokens < 1 or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("Invalid evaluation generation settings")
        self.provider, self.model = provider, model or (provider.model if provider else "fixture-dry-run")
        if provider is not None and self.model != provider.model:
            raise ValueError("Evaluation model must match the explicit provider model")
        self.service_factory = service_factory
        self.options = {"model": self.model, "base_url": provider.base_url if provider else None,
                        "temperature": temperature, "seed": seed, "max_tokens": max_tokens,
                        "stream": provider.stream if provider else False, "repetitions": repetitions,
                        "timeout_seconds": getattr(provider, "timeout", None)}

    def run(self, *, execute=False, resume=False):
        if execute and self.provider is None:
            raise ValueError("Execution requires an explicit provider and model")
        marker = self.output / "evaluation.json"
        expected = {"format": FORMAT, "synthetic": True, "data_origin": self.fixture.data_origin,
                    "fixture_digest": digest(self.fixture.model_dump()),
                    "options": self.options, "template_id": self.fixture.template_id}
        if self.output.exists() and any(self.output.iterdir()) and not marker.is_file():
            raise ValueError("Refusing to use an existing non-evaluation directory")
        self.output.mkdir(parents=True, exist_ok=True)
        with _lock(self.output / ".evaluation.lock"):
            if marker.exists():
                if json.loads(marker.read_text(encoding="utf-8")) != expected:
                    raise ValueError("Evaluation fixture/provider/model/options differ from this output")
                if not resume:
                    raise ValueError("An existing evaluation requires --resume")
            else:
                _write(marker, expected)
                _write(self.output / "fixture.json", self.fixture.model_dump())
            if execute:
                try:
                    discovery = self.provider.discover()
                    if discovery.get("requested_model_available") is not True:
                        raise ProviderFailure("requested_model_not_discovered")
                except ProviderFailure as exc:
                    reason = str(exc) if re.fullmatch(r"[a-zA-Z_]{1,80}", str(exc)) else "discovery_failure"
                    discovery = {"status": "blocked", "error_code": reason}
                    _write(self.output / "discovery.json", discovery)
                    summary = {"format": FORMAT, "mode": "execute", "synthetic": True,
                        "data_origin": self.fixture.data_origin, "status": "blocked", "stage": "discovery",
                        "trial_count": 0, "planned_trial_count": len(self.fixture.cases) * self.options["repetitions"],
                        "completed": 0, "quality_passed": None, "provider_internal_observation": "unknown",
                        "model_usage": "unknown", "measurement_status": "unknown",
                        "release_readiness": "not_established_by_this_run", "discovery": discovery, "results": []}
                    _write(self.output / "summary.json", summary)
                    return summary
                _write(self.output / "discovery.json", discovery)
            results = []
            for case in self.fixture.cases:
                for repetition in range(self.options["repetitions"]):
                    trial = self.output / (case.id + "-" + str(repetition))
                    trial.mkdir(exist_ok=True)
                    previous = sorted(trial.glob("attempt-*/result.json"))
                    if previous:
                        prior = json.loads(previous[-1].read_text(encoding="utf-8"))
                        if prior["status"] == "completed" or not execute and prior["status"] == "prepared":
                            results.append(prior)
                            continue
                        if prior["status"] == "in_progress":
                            prior.update(status="interrupted_unknown", quality={"status": "unknown", "passed": None, "axes": None})
                            _write(previous[-1], prior)
                    folders = list(trial.glob("attempt-*"))
                    folder = trial / ("attempt-" + str(len(folders) + 1).zfill(4))
                    folder.mkdir()
                    result = self._trial(case, repetition, folder, execute)
                    results.append(result)
            statuses = [row["status"] for row in results]
            quality = None
            if execute and statuses.count("completed") == len(results):
                checks = [check for row in results for check in (
                    row["quality"].get("passed"), row.get("permanent_state_unchanged"), row.get("integrity_valid"))]
                quality = False if False in checks else True if all(check is True for check in checks) else None
            required_metrics = ("prompt_tokens", "completion_tokens", "total_latency_ms") + (
                ("first_content_ms",) if self.options["stream"] else ())
            measured = [row.get("metrics", {}).get(key) for row in results for key in required_metrics]
            measurement = "complete" if execute and all(value is not None for value in measured) else (
                "partial" if any(value is not None for value in measured) else "unknown")
            summary = {"format": FORMAT, "mode": "execute" if execute else "dry_run", "synthetic": True,
                "data_origin": self.fixture.data_origin,
                "status": "blocked" if any(status not in {"completed", "prepared"} for status in statuses)
                          else "evaluated" if execute else "prepared",
                "trial_count": len(results), "completed": statuses.count("completed"),
                "quality_passed": quality,
                "measurement_status": measurement, "release_readiness": "not_established_by_this_run",
                "latency_semantics": {"total_latency_ms": "provider_http_roundtrip",
                    "first_content_ms": "first_nonempty_visible_content_delta", "first_token_ms": "unknown_internal_generation"},
                "provider_internal_observation": "unknown", "model_usage": "unknown",
                "grading": "Fixed synthetic answer fields; natural-language quality and real-world correctness are not certified",
                "results": [{key: row.get(key) for key in ("case_id", "repetition", "condition", "status", "quality", "metrics", "permanent_state_unchanged", "integrity_valid")}
                            for row in results]}
            _write(self.output / "summary.json", summary)
            return summary

    def _trial(self, case, repetition, folder, execute):
        result = {"format": FORMAT, "case_id": case.id, "repetition": repetition, "condition": case.condition,
            "data_origin": self.fixture.data_origin,
            "status": "in_progress", "quality": {"status": "not_run", "passed": None, "axes": None},
            "template_id": self.fixture.template_id, "model_id": self.model, "provider": "openai_compatible",
            "boundary": "isolated_evaluation_host", "model_usage": "unknown", "provider_internal_observation": "unknown",
            "metrics": {"prompt_tokens": None, "completion_tokens": None, "first_token_ms": None,
                        "first_content_ms": None, "total_latency_ms": None}}
        _write(folder / "result.json", result)
        try:
            with _fixture_clock(self.fixture.as_of):
                service_options = {"knowledge_mode": case.knowledge_mode} if case.knowledge_mode is not None else {}
                service = self.service_factory(folder / "fixture.db", **service_options)
                token = uuid.uuid4().hex
                credentials = [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "principal_id": "eval:owner",
                    "host_id": "eval:host", "permissions": ["read", "write", "review", "sync", "observe", "control", "learn"]}]
                with TestClient(create_app(service, credentials)) as client:
                    client.headers["Authorization"] = "Bearer " + token
                    bindings = {}
                    for step in (*self.fixture.steps, *case.steps):
                        bindings[step.name] = _call(client, step.method, _resolve(step.route, bindings), _resolve(step.body, bindings),
                                                    expected_status=step.expected_status)
                    _write(folder / "setup_receipts.json", bindings)
                    before = _permanent_state(service)
                    preparation_started = time.monotonic()
                    request = {"conversation_id": case.conversation_id, "path_revision": case.path_revision,
                        "query": case.question, "idempotency_key": "eval:run", **_resolve(case.run_options, bindings)}
                    run = _call(client, "POST", "/context/runs", request)
                    _write(folder / "run.json", run)
                    for key, expected_count in case.packet_expectations.items():
                        actual = run["data"]["packet"].get(key)
                        if not isinstance(actual, list) or len(actual) != expected_count:
                            raise ValueError("fixture_packet_expectation_failed")
                    if any(value in canonical(run["data"]["packet"]) for value in case.packet_must_not_contain):
                        raise ValueError("fixture_packet_exclusion_failed")
                    attempt_id = "eval:attempt"
                    _call(client, "POST", "/attempts", {"id": attempt_id, "run_id": run["id"],
                        "path_revision": request["path_revision"], "model_id": self.model, "packet_digest": run["data"]["packet_digest"]})
                    messages = [{"role": "system", "content": self.fixture.system_prompt}]
                    if case.inject_packet and run["data"]["prompt_text"]:
                        messages.append({"role": "system", "content": run["data"]["prompt_text"]})
                    messages.append({"role": "user", "content": case.question})
                    payload = {"model": self.model, "messages": messages, "temperature": self.options["temperature"],
                        "seed": self.options["seed"] + repetition if self.options["seed"] is not None else None,
                        "max_tokens": self.options["max_tokens"], "stream": self.options["stream"]}
                    if payload["seed"] is None:
                        payload.pop("seed")
                    if payload["stream"]:
                        payload["stream_options"] = {"include_usage": True}
                    host_form = {**payload, "params": {key: payload[key] for key in ("temperature", "seed") if key in payload}}
                    observed = final_input(host_form, run["data"]["prompt_text"], run["data"]["packet_digest"])
                    observed["path_revision"] = request["path_revision"]
                    final = _call(client, "POST", "/attempts/" + attempt_id + "/observations", observed)
                    result.update(run_id=run["id"], attempt_id=attempt_id, path_revision=request["path_revision"],
                        packet_digest=run["data"]["packet_digest"], request_digest=observed["request_digest"],
                        wire_request_digest=digest(payload), selection=run["data"].get("selected", []),
                        knowledge_selection=run["data"].get("knowledge_selection", []),
                        compiled={key: value for key, value in run["data"].items() if key.startswith("compiled_")},
                        host_finalized=final["data"], generation_settings=self.options,
                        metrics={**result["metrics"], "request_utf8_bytes": len(canonical(payload).encode("utf-8")),
                                 "context_build_ms": run["data"]["metrics"]["build_ms"],
                                 "host_prepare_ms": round((time.monotonic() - preparation_started) * 1000, 3),
                                 "packet_utf8_bytes": len(run["data"]["prompt_text"].encode("utf-8"))})
                    _write(folder / "request.json", payload)
                    _write(folder / "result.json", result)
                    if not execute:
                        result.update(status="prepared", permanent_state_unchanged=before == _permanent_state(service))
                    else:
                        dispatch = {"stage": "dispatch", "status": "started", "request_digest": observed["request_digest"],
                                    "path_revision": request["path_revision"]}
                        _call(client, "POST", "/attempts/" + attempt_id + "/observations", dispatch)
                        model_started = time.monotonic()
                        try:
                            answer = self.provider.generate(payload)
                            if answer.get("reported_model_id") != self.model:
                                raise ProviderFailure("provider_model_mismatch_or_missing")
                        except ProviderFailure as exc:
                            reason = str(exc) if re.fullmatch(r"[a-zA-Z_]{1,80}", str(exc)) else "provider_failure"
                            elapsed = round((time.monotonic() - model_started) * 1000, 3)
                            _call(client, "POST", "/attempts/" + attempt_id + "/observations", {**dispatch, "status": "failed",
                                  "metrics": {"outcome": "unknown", "reason_code": reason, "total_latency_ms": elapsed}})
                            result.update(status="provider_failed", error_code=reason,
                                quality={"status": "unknown", "passed": None, "axes": None},
                                metrics={**result["metrics"], "total_latency_ms": elapsed},
                                permanent_state_unchanged=before == _permanent_state(service))
                        else:
                            _write(folder / "response.json", answer)
                            result["metrics"].update({key: answer.get(key) for key in
                                ("prompt_tokens", "completion_tokens", "first_token_ms", "first_content_ms", "total_latency_ms")})
                            _call(client, "POST", "/attempts/" + attempt_id + "/observations", {**dispatch, "status": "completed",
                                "metrics": {key: answer.get(key) for key in ("prompt_tokens", "completion_tokens", "first_token_ms", "first_content_ms", "total_latency_ms")}})
                            if answer["content"]:
                                response_id, response_revision = "eval:response", "eval:response-revision"
                                _call(client, "POST", "/dialogue/sync", {"conversation_id": request["conversation_id"],
                                    "messages": [{"id": response_id, "role": "assistant", "content": answer["content"],
                                                  "model_provenance": {"model_id": self.model, "reported_model_id": answer.get("reported_model_id")}}],
                                    "active_event_ids": [*run["data"]["input_event_ids"], response_id],
                                    "expected_revision": request["path_revision"], "revision_key": response_revision, "auto_capture": False})
                                _call(client, "POST", "/attempts/" + attempt_id + "/complete", {
                                    "response_event_id": response_id, "path_revision": response_revision})
                                result["status"] = "completed"
                            else:
                                # The provider finished transport without an assistant message.
                                # Do not invent a response event just to complete the attempt.
                                result.update(status="provider_incomplete", error_code="empty_assistant_content")
                            result.update(quality=grade(answer["content"], case.rubric, finish_reason=answer.get("finish_reason")),
                                permanent_state_unchanged=before == _permanent_state(service))
                        result["metrics"]["host_end_to_end_ms"] = round((time.monotonic() - preparation_started) * 1000, 3)
                    result["integrity_valid"] = service.db.verify()["valid"]
        except Exception as exc:
            safe_code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("fixture_") else type(exc).__name__
            result.update(status="harness_failed", error_code=safe_code,
                          quality={"status": "unknown", "passed": None, "axes": None})
        _write(folder / "result.json", result)
        return result

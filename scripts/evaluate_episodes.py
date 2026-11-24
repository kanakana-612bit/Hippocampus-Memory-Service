"""Two-call episodic pilot using newly authored fiction and an isolated canonical DB.

No credential files, existing conversation, production API, or existing database
is accepted by this script. Provider output is retained unchanged as fictional
evaluation material. A failed first call stops the pilot without repair or retry.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from v02.api import create_app
from v02.episode_generation import EpisodeGenerator, GenerationError, ModelConfig, TransportError
from v02.episode_worker import EpisodeWorker
from v02.service import Service
import v02.episode_generation as generation


FICTION = [
    {"id": "fictional:episode-pilot:user", "role": "user",
     "content": "架空の観測塔計画では屋根を青色にすると決定した。次回は窓の形を検討する。費用はまだ未解決である。",
     "event_time": "2026-09-22T00:00:00Z"},
    {"id": "fictional:episode-pilot:assistant", "role": "assistant",
     "content": "架空の計画として、屋根の色は青、窓の形と費用は未決定と記録します。",
     "event_time": "2026-09-22T00:01:00Z"},
]

RUBRIC = {
    "input_kind": "newly_authored_fictional_dialogue",
    "technical": ["both_operations_commit_without_output_repair", "exact_quote_and_digest_validation",
                  "original_records_unchanged", "all_structured_outputs_remain_candidates", "no_knowledge_promotion"],
    "semantic_review_required": ["roof_blue_is_attributed_user_decision", "window_shape_is_future_intent_not_completed",
        "cost_remains_unresolved_without_invented_amount", "assistant_report_does_not_become_user_speech",
        "no_new_user_stance_or_fact_is_fabricated", "daily_episodes_retain_all_supplied_origins"],
    "limits": {"max_provider_calls": 2, "seconds_per_call": 120, "automatic_repair": False,
               "provider_retry": False},
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run(output: Path, model_url: str, model: str):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    save(output / "rubric.json", RUBRIC)
    save(output / "fictional-input.json", {"input_kind": RUBRIC["input_kind"], "messages": FICTION})
    fixture_root = ROOT / "tmp-tests"
    fixture_root.mkdir(exist_ok=True)
    folder = fixture_root / ("episode-pilot-" + uuid.uuid4().hex)
    folder.mkdir()
    summary = {"input_kind": RUBRIC["input_kind"], "model": model,
        "production_api_used": False, "existing_data_read": False,
        "provider_calls": 0, "steps": [], "status": "running", "semantic_review": "pending"}
    provider_request = generation.json_request
    started = time.perf_counter()
    client = None
    current_operation = None

    def capture_provider(url, **kwargs):
        if summary["provider_calls"] >= 2:
            raise GenerationError("fictional_pilot_call_budget_exhausted")
        summary["provider_calls"] += 1
        number = summary["provider_calls"]
        prefix = f"{number:02d}-{current_operation}"
        save(output / (prefix + "-request.json"), kwargs["payload"])
        call_started = time.perf_counter()
        try:
            response = provider_request(url, **kwargs)
            # Serialization preserves the provider's message content verbatim.
            save(output / (prefix + "-provider-result.json"), response)
            return response
        finally:
            summary["last_provider_latency_seconds"] = round(time.perf_counter() - call_started, 3)

    try:
        with patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""}), \
                patch("v02.episode_service.now", return_value="2026-09-22T18:00:00Z"):
            service = Service(folder / "fictional.db")
            principal, token = "fictional:episode-owner", "fictional:isolated-token"
            service.sync(principal, {"conversation_id": "fictional:episode-conversation", "revision_key": "fictional:path:1",
                "expected_revision": None, "auto_capture": False, "messages": FICTION,
                "active_event_ids": [message["id"] for message in FICTION]})
            original = deepcopy(service.list_records(principal, "event"))
            client = TestClient(create_app(service, [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "principal_id": principal, "host_id": "fictional:episode-worker", "permissions": ["read", "write"]}]))

            class CanonicalClient:
                def request(self, method, route, payload=None):
                    response = client.request(method, "/api/v0.2" + route,
                        headers={"Authorization": "Bearer " + token}, json=payload)
                    if response.status_code != 200:
                        # This error body is from our isolated fictional DB only.
                        save(output / "fictional-canonical-rejection.json", response.json())
                        raise TransportError("canonical_fixture_failure", status=response.status_code)
                    return response.json()

            generator = EpisodeGenerator(ModelConfig(model_url, model, timeout=120, max_tokens=4096))
            worker = EpisodeWorker(CanonicalClient(), generator, {"conversation_id": "fictional:episode-conversation"},
                                   reason="newly_authored_fictional_model_pilot")
            try:
                worker.seal()
                with patch("v02.episode_generation.json_request", side_effect=capture_provider):
                    for operation in ("provisional", "daily"):
                        current_operation = operation
                        step_started = time.perf_counter()
                        result = worker.process(operation, day="2026-09-22" if operation == "daily" else None)
                        summary["steps"].append({**result, "contract_grounding": "passed",
                            "latency_seconds": round(time.perf_counter() - step_started, 3)})
                        save(output / "summary.json", summary)
                summary["status"] = "evaluated"
                summary["contract_grounding"] = "passed"
            except (GenerationError, TransportError) as exc:
                code = exc.code if isinstance(exc, TransportError) else str(exc)
                summary.update(status="blocked" if isinstance(exc, TransportError) else "failed",
                    contract_grounding="not_passed", error_code=code, failed_operation=current_operation)
                if isinstance(exc, TransportError):
                    summary["http_status"] = exc.status
                    if exc.code != "canonical_fixture_failure":
                        summary["contract_grounding"] = "not_evaluated"
                summary["semantic_review"] = "not_evaluated"
            except Exception as exc:
                summary.update(status="failed", contract_grounding="not_passed", error_code=type(exc).__name__,
                               failed_operation=current_operation)
            summary["raw_unchanged"] = service.list_records(principal, "event") == original
            candidates = service.list_records(principal, "structured_candidate")
            summary["no_adoption"] = all(c["data"].get("adoption") == "candidate" for c in candidates)
            summary["no_knowledge_promotion"] = not service.list_records(principal, "knowledge_claim")
            summary["derived_counts"] = {kind: len(service.list_records(principal, kind))
                for kind in ("episode_summary", "episode", "structured_candidate")}
            save(output / "fictional-derived-records.json", {kind: service.list_records(principal, kind)
                for kind in ("episode_summary", "episode", "structured_candidate")})
            summary["technical_passed"] = (summary["status"] == "evaluated" and summary["raw_unchanged"]
                and summary["no_adoption"] and summary["no_knowledge_promotion"] and len(summary["steps"]) == 2)
    finally:
        if client is not None:
            client.close()
        target = folder.resolve()
        if target.parent != fixture_root.resolve() or not target.name.startswith("episode-pilot-"):
            raise RuntimeError("Fictional fixture cleanup boundary mismatch")
        shutil.rmtree(target)
        summary["total_latency_seconds"] = round(time.perf_counter() - started, 3)
        save(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.output, args.model_url, args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("technical_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

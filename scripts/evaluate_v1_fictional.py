"""Explicit real-model evaluation using only a checked-in fictional fixture."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v1.analysis import ProviderAnalyzer
from v1.contracts import Identity, ModelConfig, SourceRegister
from v1.learning import Learning
from v1.memory import Memory
from v1.storage import Storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, help="Must be a new directory")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context-window", required=True, type=int)
    parser.add_argument("--output-reserve", type=int, default=8192)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    model = ModelConfig(id="explicit-evaluation-model", provider="openai_compatible", endpoint=args.endpoint,
        model=args.model, context_window=args.context_window, output_reserve=args.output_reserve,
        destination="explicit-fictional-evaluation")
    storage = Storage.create(args.workspace)
    identity = Identity(principal_id="fictional-evaluation-owner", trusted_private=True,
        destination=model.destination, permissions=frozenset({"sources.write", "learning.run", "learning.commit", "records.read"}))
    learning = Learning(storage, [model], model.id, ProviderAnalyzer(timeout=120))
    # There is deliberately no arbitrary input-file or previous-DB parameter.
    raw = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v1" / "fictional_garden.json").read_bytes()
    learning.register_source(identity, SourceRegister(id="fictional-garden", original_name="fictional_garden.json",
        data_class="synthetic", allowed_destinations=(model.destination,)), raw, key="register-fictional-fixture")
    report = {"data_class": "synthetic", "fixture": "fictional_garden.json", "model": model.model,
        "model_revision": model.revision, "semantic_quality": "requires_review_of_fictional_output",
        "provider_fallback": False, "runs": []}
    for mode in ("initial", "reanalysis"):
        started = time.perf_counter()
        run = learning.create_run(identity, mode=mode, ranges=[{"source_id": "fictional-garden", "first": 1, "last": 4}], key=mode)
        results = []
        for batch_id in run["batch_ids"]:
            analyzed = learning.analyze(identity, batch_id, key=mode + "-analysis-" + batch_id)
            results.append(analyzed)
            if analyzed["status"] == "valid":
                learning.commit(identity, batch_id, key=mode + "-commit-" + batch_id)
        report["runs"].append({"mode": mode, "latency_seconds": time.perf_counter() - started, "batches": results})
        if any(r["status"] != "valid" for r in results):
            break  # Do not hide a failed selected model with retries or another provider.
    report["integrity"] = storage.verify()
    report["authorized_record_count"] = Memory(storage).timeline(identity)["total"]
    # Candidate counts do not certify semantic quality. Review the fictional staging
    # through the authorized v1 UI before setting an external evaluation judgment.
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"completed": True, "semantic_quality": report["semantic_quality"], "report_written": True}))


if __name__ == "__main__":
    main()

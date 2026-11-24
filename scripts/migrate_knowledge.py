"""Offline semantic-memory candidate migration; default action only saves a plan."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.knowledge_migration import apply, plan, save_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--manifest", required=True, help="Saved dry-run manifest; created exclusively in plan mode")
    parser.add_argument("--principal", help="Required only when preparing a plan")
    parser.add_argument("--input", help="Explicit memory/event/span/proposition mapping JSON; plan mode only")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if args.input or args.principal:
            parser.error("Apply uses only the saved manifest; do not pass new input or ownership")
        result = apply(args.db, args.manifest, args.operation_id, service_stopped=args.service_stopped)
    else:
        if not args.input or not args.principal:
            parser.error("Planning requires --input and --principal")
        inputs = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        result = plan(args.db, args.principal, args.operation_id, inputs)
        save_manifest(result, args.manifest)
        # Propositions/quotes remain in the explicitly saved local manifest.
        result = {key: result[key] for key in ("operation_id", "manifest_digest", "ready_count", "quarantined_count", "applied")}
        result["manifest_saved"] = str(Path(args.manifest).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

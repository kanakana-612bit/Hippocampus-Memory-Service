"""Explicit offline canonical schema upgrades; defaults to a read-only schema 5 plan."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.schema_upgrade import upgrade


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--target-schema", choices=("2", "3", "4", "5"), default="5",
                        help="Current target 5; retain original target 2/3/4 when replaying an earlier operation")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    result = upgrade(args.db, args.operation_id, target_schema=args.target_schema,
                     apply=args.apply, service_stopped=args.service_stopped)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit offline erasure of one entire principal; default is plan-only."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.maintenance import erase_principal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    parser.add_argument("--confirm-principal")
    parser.add_argument("--purge-managed-backups", action="store_true",
                        help="Explicitly remove ALL managed backups, including snapshots containing other principals")
    args = parser.parse_args()
    result = erase_principal(args.db, args.principal, args.operation_id, apply=args.apply,
        service_stopped=args.service_stopped, confirm_principal=args.confirm_principal,
        purge_managed_backups=args.purge_managed_backups)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.apply or result.get("complete") else 2


if __name__ == "__main__":
    raise SystemExit(main())

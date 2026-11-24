"""Offline migration; dry-run by default. Never starts or modifies a running service."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.migration import migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.destination, args.principal, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

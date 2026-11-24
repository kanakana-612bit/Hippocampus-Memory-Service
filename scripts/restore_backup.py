"""Explicit offline Ver0.2 restore; legacy database settings are not accepted."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.maintenance import restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('filename', help='Signed .db filename in the canonical managed backup directory')
    parser.add_argument('--db', default=os.getenv('HIPPOCAMPUS_V02_DB'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--service-stopped', action='store_true')
    args = parser.parse_args()
    if not args.db:
        parser.error('Configure --db or HIPPOCAMPUS_V02_DB')
    result = restore(args.db, args.filename, apply=args.apply, service_stopped=args.service_stopped)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('valid', result.get('verification', {}).get('valid', False)) else 2


if __name__ == '__main__':
    raise SystemExit(main())

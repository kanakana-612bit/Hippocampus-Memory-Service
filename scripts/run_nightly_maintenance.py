"""Canonical authenticated nightly client. Old extraction/budget flags are rejected."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.host import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-file', default=os.getenv('HIPPOCAMPUS_V02_CLIENT_FILE'))
    parser.add_argument('--user-id')
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--conversation-id')
    scope.add_argument('--all-conversations', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.client_file:
        parser.error('Configure --client-file or HIPPOCAMPUS_V02_CLIENT_FILE')
    config = json.loads(Path(args.client_file).read_text(encoding='utf-8-sig'))
    users = config['users']
    if args.user_id:
        users = {args.user_id: users[args.user_id]}
    results = []
    for user, entry in users.items():
        client = Client(config['url'], entry['token'], entry['principal_id'])
        paths = client.request('GET', '/records?kind=path')
        conversations = [p['conversation_id'] for p in paths
                         if not args.conversation_id or p['conversation_id'] == args.conversation_id]
        for conversation in conversations:
            result = {'dry_run': True} if args.dry_run else client.request('POST', '/nightly/run', {'conversation_id': conversation})
            results.append({'principal_id': entry['principal_id'], 'conversation_id': conversation, 'result': result})
    print(json.dumps({'mode': 'canonical_deterministic_reassessment', 'results': results}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

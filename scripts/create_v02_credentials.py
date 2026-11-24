"""Create new local credentials without displaying tokens; never overwrites existing files."""
import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--client-file", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8091")
    args = parser.parse_args()
    auth, client = Path(args.auth_file).resolve(), Path(args.client_file).resolve()
    if auth == client or auth.exists() or client.exists():
        parser.error("Distinct, new destination files are required")
    token = secrets.token_urlsafe(48)
    server = [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "principal_id": args.principal,
               "host_id": args.host_id, "permissions": ["read", "write", "sync", "observe"]}]
    frontend = {"url": args.url, "users": {args.user_id: {"token": token, "principal_id": args.principal}}}
    for path, value in ((auth, server), (client, frontend)):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    print("Created local server/client registries. No control, review, learn or admin permissions granted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

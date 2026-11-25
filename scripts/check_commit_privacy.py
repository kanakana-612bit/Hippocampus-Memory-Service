"""Fail closed on publishable Git metadata and content; never print private values."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from check_public_tree import EMAIL, NOREPLY, check_tree, git_bytes


def require_message(data: bytes) -> None:
    text = data.decode("utf-8", errors="strict")
    if any(not NOREPLY.fullmatch(value) for value in EMAIL.findall(text)):
        raise ValueError("Metadata contains a non-noreply email; value suppressed")


def require_ident(identity: bytes) -> None:
    text = identity.decode("utf-8", errors="strict")
    match = re.fullmatch(r"[^<>@\r\n]+ <([^<>\r\n]+)> -?\d+ [+-]\d{4}", text.strip())
    if not match or not NOREPLY.fullmatch(match.group(1)):
        raise ValueError("Author/committer/tagger must use GitHub noreply; value suppressed")
    # Also reject a second address disguised as part of the display name.
    require_message(identity)


def validate_repository(root: Path) -> None:
    if git_bytes(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("Full history is required; shallow repository rejected")
    grafts = git_bytes(root, "rev-parse", "--git-path", "info/grafts").decode().strip()
    if (root / grafts).exists() or os.getenv("GIT_GRAFT_FILE"):
        raise ValueError("Grafted history cannot be checked")
    if os.getenv("GIT_SHALLOW_FILE"):
        raise ValueError("Alternate shallow history cannot be checked")


def check_object(root: Path, ref: str, branch: str = "main", *, content: bool = True) -> int:
    validate_repository(root)
    sha = git_bytes(root, "rev-parse", "--verify", "--end-of-options", ref).decode().strip()
    kind = git_bytes(root, "cat-file", "-t", sha).strip()
    while kind == b"tag":
        raw = git_bytes(root, "cat-file", "tag", sha)
        header, message = raw.split(b"\n\n", 1)
        taggers = [line[7:] for line in header.splitlines() if line.startswith(b"tagger ")]
        if len(taggers) != 1:
            raise ValueError("Tagger identity is missing or duplicated")
        require_ident(taggers[0])
        require_message(raw)
        targets = [line[7:] for line in header.splitlines() if line.startswith(b"object ")]
        if len(targets) != 1:
            raise ValueError("Malformed tag target")
        sha = targets[0].decode("ascii")
        kind = git_bytes(root, "cat-file", "-t", sha).strip()
    if kind != b"commit":
        raise ValueError("Publication refs must resolve to commits")
    commits = git_bytes(root, "rev-list", sha).decode().splitlines()
    seen: set = set()
    for commit in commits:
        raw = git_bytes(root, "cat-file", "commit", commit)
        header, message = raw.split(b"\n\n", 1)
        for kind in (b"author ", b"committer "):
            identities = [line[len(kind):] for line in header.splitlines() if line.startswith(kind)]
            if len(identities) != 1:
                raise ValueError("Commit identity is missing or duplicated")
            require_ident(identities[0])
        require_message(raw)
        if content:
            check_tree(root, commit, branch, seen)
    return len(commits)


def push(root: Path, lines) -> None:
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Malformed pre-push input")
        _, sha, remote_ref, _ = fields
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValueError("Invalid object ID")
        if set(sha) == {"0"}:
            continue
        # Never publish replacement refs, notes, audit backup refs or an address in a ref.
        require_message(remote_ref.encode())
        if not remote_ref.startswith(("refs/heads/", "refs/tags/")):
            raise ValueError("Only branch/tag publication is permitted")
        branch = "Private" if remote_ref == "refs/heads/Private" else "main"
        check_object(root, sha, branch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["identity", "index", "message", "check", "push"])
    parser.add_argument("target", nargs="?", default="HEAD")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        if args.action in {"identity", "index"}:
            for role in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
                require_ident(git_bytes(root, "var", role))
            if args.action == "index":
                check_tree(root, None, args.branch)
        elif args.action == "message":
            require_message(Path(args.target).read_bytes())
        elif args.action == "check":
            count = check_object(root, args.target, args.branch)
            print("Publication privacy guard passed:", count, "reachable commits")
        elif args.action == "push":
            push(root, sys.stdin)
    except (UnicodeError, OSError, subprocess.SubprocessError):
        print("Publication blocked: input/object access failed; details suppressed.", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as error:
        # These controlled errors contain reason codes, not Git stderr or private values.
        print("Publication blocked:", str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

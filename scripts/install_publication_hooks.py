"""Install clone-local publication guards without changing global Git settings."""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import subprocess
import sys

from check_public_tree import NOREPLY, git_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    if not NOREPLY.fullmatch(args.email) or any(c in args.name for c in "<>\r\n@"):
        parser.error("Use a display name and a GitHub noreply email")
    root = Path(git_bytes(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip()).resolve()
    hook_dir = Path(git_bytes(root, "rev-parse", "--git-path", "hippocampus-privacy-hooks").decode().strip())
    hook_dir = (root / hook_dir).resolve()
    hook_dir.mkdir(parents=True, exist_ok=True)
    guard = root / "scripts/check_commit_privacy.py"
    command = " ".join(shlex.quote(p.as_posix()) for p in (Path(sys.executable).resolve(), guard))
    for name, action in {"pre-commit": "index", "commit-msg": 'message "$1"', "pre-push": "push"}.items():
        path = hook_dir / name
        path.write_text("#!/bin/sh\nexec " + command + " " + action + "\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)
    for key, value in {"user.name": args.name, "user.email": args.email,
                       "user.useConfigOnly": "true", "core.hooksPath": hook_dir.as_posix(),
                       "push.default": "simple"}.items():
        subprocess.run(["git", "config", "--local", key, value], cwd=root, check=True)
    print("Installed pre-commit, commit-msg and pre-push privacy guards for this clone.")


if __name__ == "__main__":
    main()

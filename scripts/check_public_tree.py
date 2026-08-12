"""Reject runtime data and likely private artifacts from the Git tree."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


FORBIDDEN_PARTS = {
    ".cache",
    ".venv",
    "__pycache__",
    "backups",
    "cache",
    "data",
    "exports",
    "generated",
    "generations",
    "logs",
    "media",
    "outputs",
    "recordings",
    "tmp",
    "tmp-tests",
    "uploads",
    "venv",
}

FORBIDDEN_SUFFIXES = {
    ".7z",
    ".backup",
    ".bak",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".jsonl",
    ".log",
    ".mp3",
    ".mp4",
    ".ndjson",
    ".ogg",
    ".png",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".wav",
    ".webm",
    ".webp",
    ".zip",
}

CONTENT_RULES = {
    "Windows user profile path": re.compile(r"[A-Za-z]:\\Users\\[^\r\n]+", re.I),
    "Codex attachment path": re.compile(r"\.codex[\\/]attachments[\\/]", re.I),
    "OpenWebUI chat URL": re.compile(
        r"https?://[^\s]+/c/[0-9a-f]{8}-[0-9a-f-]{27,}", re.I
    ),
    "private key material": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
}

CONTENT_SCAN_EXEMPT = {
    "scripts/check_public_tree.py",
}

MAIN_FORBIDDEN_PATHS = {
    "docs/langgraph-integration-plan.ja.md",
}

MAIN_FORBIDDEN_PREFIXES = (
    "docs/private/",
    "integrations/private/",
    "private/",
)


def git_output(root: Path, *args: str) -> list[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or "Git command failed."
        raise RuntimeError(detail)
    return [line for line in process.stdout.splitlines() if line]


def repository_root(script_root: Path) -> Path:
    process = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=script_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        raise RuntimeError("No active Git repository was found for this checkout.")
    return Path(process.stdout.strip()).resolve()


def tracked_files(root: Path, staged: bool) -> list[str]:
    if staged:
        return git_output(
            root,
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
        )
    return git_output(root, "ls-files")


def target_branch(root: Path) -> str:
    # Pull requests are checked against their target branch. Pushes use the
    # checked-out branch name, with local Git as the final fallback.
    return (
        os.getenv("GITHUB_BASE_REF")
        or os.getenv("GITHUB_REF_NAME")
        or git_output(root, "rev-parse", "--abbrev-ref", "HEAD")[0]
    )


def media_is_curated(path: PurePosixPath) -> bool:
    parts = path.parts
    return len(parts) >= 3 and parts[0:2] == ("docs", "assets")


def path_findings(path_text: str) -> list[str]:
    normalized = path_text.replace("\\", "/")
    path = PurePosixPath(normalized)
    findings: list[str] = []

    if any(part.lower() in FORBIDDEN_PARTS for part in path.parts):
        findings.append("runtime/private data directory")

    lower_name = path.name.lower()
    suffix = "".join(path.suffixes).lower()
    matched_suffix = next(
        (
            candidate
            for candidate in FORBIDDEN_SUFFIXES
            if lower_name.endswith(candidate)
        ),
        None,
    )
    if matched_suffix and not media_is_curated(path):
        findings.append(f"forbidden artifact type ({matched_suffix})")

    if lower_name == ".env" or (
        lower_name.startswith(".env.") and lower_name != ".env.example"
    ):
        findings.append("local environment file")

    if suffix in {".tar.gz", ".tar.bz2", ".tar.xz"}:
        findings.append(f"archive artifact ({suffix})")

    return findings


def content_findings(root: Path, path_text: str) -> list[str]:
    normalized = path_text.replace("\\", "/")
    if normalized in CONTENT_SCAN_EXEMPT:
        return []

    path = root / normalized
    if not path.is_file() or path.stat().st_size > 2_000_000:
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    return [name for name, rule in CONTENT_RULES.items() if rule.search(content)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check tracked files for private or generated runtime data."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Check only files staged for the next commit.",
    )
    args = parser.parse_args()

    try:
        root = repository_root(Path(__file__).resolve().parent.parent)
        paths = tracked_files(root, args.staged)
        branch = target_branch(root)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    violations: list[tuple[str, str]] = []
    for path_text in paths:
        normalized = path_text.replace("\\", "/")
        if branch == "main" and (
            normalized in MAIN_FORBIDDEN_PATHS
            or normalized.startswith(MAIN_FORBIDDEN_PREFIXES)
        ):
            violations.append((path_text, "project-specific content on main"))
        for finding in path_findings(path_text):
            violations.append((path_text, finding))
        for finding in content_findings(root, path_text):
            violations.append((path_text, finding))

    if violations:
        print("Publication guard rejected the following tracked content:")
        for path_text, reason in violations:
            print(f"  - {path_text}: {reason}")
        print("Remove the data from Git history or replace it with a sanitized fixture.")
        return 1

    scope = "staged" if args.staged else "tracked"
    print(
        f"Publication guard passed for {len(paths)} {scope} files "
        f"on target branch {branch}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

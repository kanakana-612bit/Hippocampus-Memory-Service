"""Reject runtime data and likely private artifacts from the Git tree."""

from __future__ import annotations

import argparse
import ast
import base64
import json
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
    ".p12",
    ".pem",
    ".pfx",
    ".png",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".wav",
    ".webm",
    ".webp",
    ".zip",
    ".key",
    ".aac", ".aiff", ".aif", ".amr", ".m4a", ".opus", ".wma",
    ".mkv", ".mov", ".avi", ".gz", ".bz2", ".xz",
}

CONTENT_RULES = {
    "Windows user profile path": re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\r\n]+", re.I),
    "Codex attachment path": re.compile(r"\.codex[\\/]attachments[\\/]", re.I),
    "OpenWebUI chat URL": re.compile(
        r"https?://[^\s]+/c/[0-9a-f]{8}-[0-9a-f-]{27,}", re.I
    ),
    "private key material": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
}

EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOREPLY = re.compile(r"(?:[A-Za-z0-9+_.-]+@users\.noreply\.github\.com|noreply@github\.com)\Z")

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


def target_branch(root: Path) -> str:
    # Pull requests are checked against their target branch. Pushes use the
    # checked-out branch name, with local Git as the final fallback.
    return (
        os.getenv("GITHUB_BASE_REF")
        or os.getenv("GITHUB_REF_NAME")
        or git_output(root, "rev-parse", "--abbrev-ref", "HEAD")[0]
    )


def path_findings(path_text: str) -> list[str]:
    normalized = path_text.replace("\\", "/")
    path = PurePosixPath(normalized)
    findings: list[str] = []

    if any(not permitted_content_email(email) for email in EMAIL.findall(normalized)):
        findings.append("non-example, non-noreply email in path; value suppressed")

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
    if matched_suffix:
        findings.append(f"forbidden artifact type ({matched_suffix})")

    if lower_name == ".env" or (
        lower_name.startswith(".env.") and lower_name != ".env.example"
    ):
        findings.append("local environment file")

    if suffix in {".tar.gz", ".tar.bz2", ".tar.xz"}:
        findings.append(f"archive artifact ({suffix})")

    return findings


def permitted_content_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[1].lower()
    return bool(NOREPLY.fullmatch(email)) or domain in {
        "example.com", "example.org", "example.net", "example.invalid",
    } or domain.endswith((".invalid", ".test"))


def binary_signature(data: bytes) -> bool:
    if len(data) < 12:
        return False
    return (
        (data[:4] in (b"RIFF", b"RIFX", b"RF64") and data[8:12] == b"WAVE")
        or data.startswith((b"fLaC", b"OggS", b"ID3", b"#!AMR", b"\x1aE\xdf\xa3",
                            b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c",
                            b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"))
        or (data.startswith(b"FORM") and data[8:12] in (b"AIFF", b"AIFC"))
        or data[4:8] == b"ftyp"
        or (len(data) > 3 and data[0] == 255 and data[1] & 0xe0 == 0xe0)
    )


def literal_strings(content: str, path: str):
    yield content
    if path.endswith(".json"):
        def walk(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, item in value.items():
                    yield from walk(key)
                    yield from walk(item)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)
        try:
            yield from walk(json.loads(content))
        except ValueError:
            pass
    if path.endswith(".py"):
        try:
            for node in ast.walk(ast.parse(content)):
                if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                    yield node.value
        except SyntaxError:
            pass


def content_findings(data: bytes, path_text: str) -> list[str]:
    findings = []
    if binary_signature(data):
        findings.append("media/archive signature")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return findings + ["non-UTF8 data requires separate review"]
    if "\0" in content:
        findings.append("binary NUL byte")
    if content.startswith("version https://git-lfs.github.com/spec/v1"):
        findings.append("uninspected LFS payload")
    for value in literal_strings(content, path_text):
        if isinstance(value, bytes):
            if binary_signature(value):
                findings.append("media/archive bytes literal")
            continue
        findings.extend(name for name, rule in CONTENT_RULES.items() if rule.search(value))
        if any(not permitted_content_email(email) for email in EMAIL.findall(value)):
            findings.append("non-example, non-noreply email; value suppressed")
        if re.search(r"data:[a]udio/", value, re.I):
            findings.append("audio data URI")
        for match in re.finditer(r"(?<![A-Za-z0-9+/_-])(?:[A-Za-z0-9+/_-]{4}[ \t\r\n]*){16,}(?:[A-Za-z0-9+/_-]{2,3}={1,2})?", value):
            encoded = re.sub(r"\s+", "", match.group())
            try:
                raw = base64.b64decode(encoded + "=" * ((-len(encoded)) % 4), altchars=b"-_", validate=True)
            except ValueError:
                continue
            if binary_signature(raw):
                findings.append("encoded media/archive signature")
    return sorted(set(findings))


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "--no-replace-objects", *args], cwd=root,
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Git object access failed; details suppressed")
    return result.stdout


def check_tree(root: Path, ref: str | None, branch: str, seen: set | None = None) -> int:
    # Read the actual index or immutable tree, never the worktree overlay.
    seen = seen if seen is not None else set()
    entries = git_bytes(root, "ls-tree", "-r", "-z", ref) if ref else git_bytes(root, "ls-files", "--stage", "-z")
    count = 0
    for row in entries.split(b"\0"):
        if not row:
            continue
        metadata, path_bytes = row.split(b"\t", 1)
        path = path_bytes.decode("utf-8", errors="strict")
        fields = metadata.decode("ascii").split()
        mode, oid = (fields[0], fields[2]) if ref else (fields[0], fields[1])
        if not ref and fields[2] != "0":
            raise RuntimeError("Unmerged index cannot be checked")
        if mode not in {"100644", "100755"}:
            raise RuntimeError("Symlink/submodule/non-regular entry requires separate review")
        findings = path_findings(path)
        if branch == "main" and (path in MAIN_FORBIDDEN_PATHS or path.startswith(MAIN_FORBIDDEN_PREFIXES)):
            findings.append("project-specific content on main")
        if findings:
            raise RuntimeError("Publication path rejected: " + ", ".join(findings) + "; path suppressed")
        key = (oid, path)
        if key in seen:
            continue
        findings = content_findings(git_bytes(root, "cat-file", "blob", oid), path)
        if findings:
            raise RuntimeError("Blob " + oid[:12] + " rejected: " + ", ".join(findings))
        seen.add(key)
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check tracked files for private or generated runtime data."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Check the entire staged tree (also the default).",
    )
    parser.add_argument("--ref", help="Check this immutable commit/tree instead of the index.")
    args = parser.parse_args()

    try:
        root = repository_root(Path(__file__).resolve().parent.parent)
        branch = target_branch(root)
        count = check_tree(root, args.ref, branch)
    except (RuntimeError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    scope = "committed" if args.ref else "staged"
    print(
        f"Publication guard passed for {count} {scope} files "
        f"on target branch {branch}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

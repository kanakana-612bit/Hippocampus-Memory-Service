from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_manager import MemoryManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and restore a Hippocampus backup while the service is stopped."
    )
    parser.add_argument("filename", help="Backup .db filename in HIPPOCAMPUS_BACKUP_DIR")
    parser.add_argument("--db", help="Database path; defaults to HIPPOCAMPUS_DB")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the verified plan. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--service-stopped",
        action="store_true",
        help="Confirm that no Hippocampus process is using the database.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.db:
        os.environ["HIPPOCAMPUS_DB"] = args.db
    manager = MemoryManager(args.db)
    previous_anchor = manager.layered.security.read_anchor()
    if previous_anchor is None:
        raise RuntimeError("The current external anchor is missing")
    with manager.connect() as con:
        plan = manager.layered.backups.plan_restore(args.filename, con)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["valid"] else 2
    if not args.service_stopped:
        raise RuntimeError("Use --service-stopped after stopping the Hippocampus service")
    if not plan["valid"]:
        raise RuntimeError("The restore plan is not valid")

    probe = sqlite3.connect(manager.db_path, timeout=1.0)
    try:
        probe.execute("BEGIN EXCLUSIVE")
        probe.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeError("The database is busy; stop the service before restoring") from exc
    finally:
        probe.close()

    result = manager.layered.backups.restore_offline(
        args.filename,
        previous_anchor=previous_anchor,
    )
    restored = MemoryManager(manager.db_path)
    if plan["requires_new_branch"]:
        branch = restored.adopt_restore_branch(previous_anchor)
    else:
        branch = None
        with restored.connect() as con:
            anchor = restored.layered.security.anchor_document(con)
        restored.layered.security.write_anchor(anchor)
    verification = restored.phase5_status()
    output = {
        "restored": result["restored"],
        "filename": result["filename"],
        "recovery_filename": result["recovery_filename"],
        "relation": plan["relation"],
        "branch": branch,
        "phase5_complete": verification["complete"],
        "verification": verification["verification"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if verification["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

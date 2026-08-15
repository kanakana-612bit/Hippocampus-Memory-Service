from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_manager import MemoryManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Hippocampus nightly extraction and maintenance.")
    parser.add_argument("--since-hours", type=int, default=36)
    parser.add_argument("--conversation-id")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--model")
    parser.add_argument("--auto-consolidate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manager = MemoryManager()
    try:
        result = manager.run_nightly_cycle(
            since_hours=args.since_hours,
            conversation_id=args.conversation_id,
            limit=args.limit,
            model=args.model,
            auto_consolidate=args.auto_consolidate,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

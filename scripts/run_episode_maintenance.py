"""Opt-in canonical multiresolution episodic maintenance; no production defaults."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.episode_worker import main


if __name__ == "__main__":
    raise SystemExit(main())

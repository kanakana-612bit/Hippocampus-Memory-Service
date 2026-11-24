"""K5 isolated fixture evaluation. Explicit discovery, preparation, or model execution."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v02.evaluation import EvaluationRunner, OpenAICompatibleProvider, ProviderFailure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--api-key-env")
    key.add_argument("--api-key-file", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if bool(args.base_url) != bool(args.model) or (args.execute or args.discover) and not args.base_url:
        parser.error("Provide --base-url and --model together for discovery or execution")
    if not args.discover and (args.fixture is None or args.output is None):
        parser.error("--fixture and --output are required")
    if (args.api_key_env or args.api_key_file) and not args.base_url:
        parser.error("A credential requires an explicit provider")
    try:
        credential = os.environ.get(args.api_key_env) if args.api_key_env else (
            args.api_key_file.read_text(encoding="utf-8-sig").strip() if args.api_key_file else None)
        if (args.api_key_env or args.api_key_file) and not credential:
            raise ValueError("The requested credential source is empty")
        provider = OpenAICompatibleProvider(args.base_url, args.model, api_key=credential,
            timeout=args.timeout, stream=args.stream) if args.base_url else None
        if args.discover:
            result = provider.discover()
        else:
            fixture = json.loads(args.fixture.read_text(encoding="utf-8-sig"))
            result = EvaluationRunner(fixture, args.output, provider=provider, model=args.model,
                temperature=args.temperature, seed=args.seed, max_tokens=args.max_tokens,
                repetitions=args.repetitions).run(execute=args.execute, resume=args.resume)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("quality_passed") is False:
            return 1
        if args.execute and result.get("quality_passed") is None:
            return 2
        return 0 if result["status"] in {"available", "prepared", "evaluated"} else 2
    except (ValueError, OSError, ProviderFailure) as exc:
        # Do not print request/provider bodies or credential-bearing exception strings.
        safe_code = str(exc) if isinstance(exc, ProviderFailure) and re.fullmatch(r"[a-zA-Z_]{1,80}", str(exc)) else None
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "error_code": safe_code, "quality_passed": None}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

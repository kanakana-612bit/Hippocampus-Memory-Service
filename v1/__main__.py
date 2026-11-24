"""Explicit developer CLI. No access to ambient or previous deployment settings."""
import argparse
import json
from pathlib import Path

from .contracts import ModelConfig
from .storage import Storage


def main():
    parser = argparse.ArgumentParser(description="Independent synthetic-only Hippocampus Ver1.0")
    parser.add_argument("command", choices=("init", "verify", "serve"))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--config", help="Explicit new v1 service config, never an old .env")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if args.command == "init":
        storage = Storage.create(args.workspace)
        print(json.dumps({"epoch": storage.epoch, "data_class": "synthetic", "created": True}))
    else:
        storage = Storage(args.workspace)
        if args.command == "verify":
            print(json.dumps(storage.verify()))
        else:
            if not args.config:
                parser.error("serve requires an explicit independent --config")
            config = json.loads(Path(args.config).read_text(encoding="utf-8"))
            if config.get("schema") != "hippocampus.service.v1" or config.get("data_class") != "synthetic":
                parser.error("Only an explicit v1 synthetic service configuration is supported")
            from .api import create_app
            import uvicorn
            app = create_app(storage, models=[ModelConfig.model_validate(m) for m in config["models"]],
                default_model=config["default_model"], credentials=config["credentials"])
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()

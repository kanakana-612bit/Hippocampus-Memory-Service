"""Temporary UI QA server using only the explicit fictional fixture and test double."""
import argparse
import json
import secrets
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v1.api import create_app
from v1.contracts import Identity, ModelConfig, SourceRegister
from v1.learning import Learning
from v1.storage import Storage
from test_v1_learning import FictionAnalyzer

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", required=True)
parser.add_argument("--ready", required=True)
args = parser.parse_args()
storage = Storage.create(args.workspace)
identity = Identity(principal_id="fictional-ui-owner", trusted_private=True,
    permissions=frozenset({"sources.write", "learning.run", "learning.commit", "learning.review", "records.read", "contexts.write", "time.write"}))
model = ModelConfig(id="fixture", provider="fixture", model="架空データ用テスト代替器（LLMではありません）", context_window=65536)
analyzer = FictionAnalyzer()
learning = Learning(storage, [model], model.id, analyzer)
raw = (Path(__file__).parent / "fixtures" / "v1" / "fictional_garden.json").read_bytes()
learning.register_source(identity, SourceRegister(id="fictional-garden", original_name="fictional_garden.json", data_class="synthetic"), raw, key="fiction-source")
run = learning.create_run(identity, mode="initial", ranges=[{"source_id": "fictional-garden", "first": 1, "last": 4}], key="fiction-run")
for batch_id in run["batch_ids"]:
    learning.analyze(identity, batch_id, key="fiction-analyze")
    learning.commit(identity, batch_id, key="fiction-commit")
token = secrets.token_urlsafe(32)
app = create_app(storage, models=[model], default_model=model.id, credentials={token: identity}, analyzer=analyzer)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
Path(args.ready).write_text(json.dumps({"port": port, "token": token, "workspace": str(storage.root)}), encoding="utf-8")
import uvicorn
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False))
server.run(sockets=[sock])

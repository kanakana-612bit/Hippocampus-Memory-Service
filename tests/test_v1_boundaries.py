"""Fictional fault injection, new HTTP boundary and independent recovery."""
import json
import shutil
import uuid
from pathlib import Path
from unittest import TestCase, mock

from fastapi.testclient import TestClient
import test_v1_learning as fixtures
from v1.api import create_app
from v1.common import Conflict, Denied, Invalid, canonical
from v1.contracts import ModelConfig
from v1.memory import Memory
from v1.operations import backup, erase_synthetic_workspace, restore, verify_backup
from v1.storage import Storage, Transaction


class BoundaryCase(TestCase):
    setUp = fixtures.V1Case.setUp
    tearDown = fixtures.V1Case.tearDown
    source = fixtures.V1Case.source
    run_learning = fixtures.V1Case.run_learning

    def test_http_end_to_end_and_legacy_routes_absent(self):
        token = "fictional-token-for-test-only-123456789"
        other = "other-fictional-token-test-123456789"
        app = create_app(self.storage, models=[self.model], default_model=self.model.id,
            credentials={token: self.owner, other: self.owner.model_copy(update={"principal_id": "stranger"})}, analyzer=self.analyzer)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/meta").status_code, 401)
            self.assertEqual(client.get("/v02/health").status_code, 404)
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(client.get("/api/v1/meta", headers=headers).json()["real_intake_enabled"], False)
            result = client.post("/api/v1/learning/sources", headers=headers, json={"key": "source",
                "registration": {"id": "fiction-source", "original_name": "fiction.json", "data_class": "synthetic"},
                "raw_json": canonical({"messages": [{"role": "user", "text": "架空の植物観察"}, {"role": "agent", "text": "架空の応答"}]})})
            self.assertEqual(result.status_code, 200, result.text)
            result = client.post("/api/v1/learning/runs", headers=headers, json={"key": "run", "mode": "initial",
                "ranges": [{"source_id": "fiction-source", "first": 1, "last": 2}]})
            self.assertEqual(result.status_code, 200, result.text)
            batch_id = result.json()["batch_ids"][0]
            for action in ("analyze", "commit"):
                result = client.post(f"/api/v1/learning/batches/{batch_id}/{action}", headers=headers, json={"key": action})
                self.assertEqual(result.status_code, 200, result.text)
            history = client.get("/api/v1/dialogue/timeline", headers=headers)
            self.assertEqual(history.json()["total"], 3)
            self.assertEqual(history.headers["cache-control"], "no-store")
            denied = client.get("/api/v1/learning/staging/" + batch_id, headers={"Authorization": "Bearer " + other})
            self.assertEqual(denied.status_code, 403)
            invalid = client.post("/api/v1/learning/sources", headers=headers, json={"raw_json": "DO_NOT_ECHO_FICTIONAL_TEXT"})
            self.assertNotIn("DO_NOT_ECHO_FICTIONAL_TEXT", invalid.text)
            self.assertIn("架空データ", client.get("/").text)
            from datetime import datetime, timedelta, timezone
            result = client.post("/api/v1/situations", headers=headers, json={"key": "guest", "room_name": "架空の公開室",
                "audience": "guest", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.get("/api/v1/situations/current", headers=headers).json()["audience"], "guest")

    def test_transaction_failure_does_not_publish_or_advance(self):
        self.source()
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="run")
        batch_id = run["batch_ids"][0]
        self.learning.analyze(self.owner, batch_id, key="analyze")
        before = self.storage.verify()["transactions"]
        original = Transaction.insert
        def fail_late(tx, table, data):
            if table == "learning_intervals":
                raise RuntimeError("fictional commit fault")
            return original(tx, table, data)
        with mock.patch.object(Transaction, "insert", fail_late):
            with self.assertRaises(RuntimeError):
                self.learning.commit(self.owner, batch_id, key="commit")
        self.assertEqual(self.storage.verify()["transactions"], before)
        self.assertEqual(Memory(self.storage).timeline(self.owner)["total"], 0)
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 0)
        self.learning.commit(self.owner, batch_id, key="commit")
        self.assertEqual(Memory(self.storage).timeline(self.owner)["total"], 3)

    def test_multi_batch_initial_release_is_hidden_until_complete(self):
        self.source(messages=[m for i in range(4) for m in ({"role": "user", "text": "fictional observation " + str(i) + "a" * 1800}, {"role": "agent", "text": "fictional response " + "b" * 1800})])
        model = self.model.model_copy(update={"context_window": 30000, "output_reserve": 512})
        self.learning.models[model.id] = model
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 8}], key="multi")
        self.assertGreater(len(run["batch_ids"]), 1)
        for index, batch_id in enumerate(run["batch_ids"]):
            self.learning.analyze(self.owner, batch_id, key="analyze-" + batch_id)
            self.learning.commit(self.owner, batch_id, key="commit-" + batch_id)
            if index < len(run["batch_ids"]) - 1:
                self.assertEqual(Memory(self.storage).timeline(self.owner)["total"], 0)
                self.assertEqual(Memory(self.storage).contexts(self.owner)["contexts"], [])
                with self.storage.connection() as con:
                    self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 0)
        self.assertGreater(Memory(self.storage).timeline(self.owner)["total"], 8)

    def test_manual_membership_survives_reanalysis(self):
        self.source()
        self.run_learning()
        memory = Memory(self.storage)
        event = memory.timeline(self.owner, kind="event")["items"][0]
        mapping = memory.contexts(self.owner)
        context = mapping["contexts"][0]
        memory.assign_context(self.owner, event["id"], context_id=context["id"], weight=.95, confidence=None,
            expected_map_version=mapping["head_version"], expected_record_version=event["version"], reason="架空の手動分類", key="manual")
        original = self.analyzer.analyze
        def reclassify(batch, config):
            value = original(batch, config)
            value["memberships"].append({"event_id": event["id"], "context_id": context["id"], "membership_weight": .1,
                                        "source_spans": value["memories"][0]["source_spans"]})
            return value
        self.analyzer.analyze = reclassify
        self.run_learning(mode="reanalysis")
        assignments = memory.contexts(self.owner)["memberships"]
        actual = next(m for m in assignments if m["target_id"] == event["id"] and m["context_id"] == context["id"])
        self.assertEqual((actual["membership_weight"], actual["source"]), (.95, "manual"))

    def test_signed_backup_restore_and_no_live_overwrite(self):
        self.source()
        self.run_learning()
        result = backup(self.storage, self.owner, key="backup")
        restored_root = self.root.parent / ("restored-" + uuid.uuid4().hex)
        try:
            recovered = restore(result["path"], restored_root, trusted_public_key=result["trusted_public_key"])
            self.assertTrue(recovered.verify()["valid"])
            self.assertEqual(Memory(recovered).timeline(self.owner)["total"], 3)
            with self.assertRaises(Invalid):
                restore(result["path"], self.root, trusted_public_key=result["trusted_public_key"])
            keyfile = Path(result["path"]) / "signing.key"
            original = keyfile.read_bytes()
            keyfile.write_bytes(b"fictional tampering")
            with self.assertRaises(Invalid):
                verify_backup(result["path"], result["trusted_public_key"])
            keyfile.write_bytes(original)
        finally:
            if restored_root.exists():
                shutil.rmtree(restored_root)

    def test_explicit_workspace_erasure_includes_local_backups(self):
        self.source()
        self.run_learning()
        backup(self.storage, self.owner, key="backup")
        with self.assertRaises(Invalid):
            erase_synthetic_workspace(self.storage, self.owner, expected_epoch=self.storage.epoch, confirm="")
        result = erase_synthetic_workspace(self.storage, self.owner, expected_epoch=self.storage.epoch, confirm="ERASE_SYNTHETIC_WORKSPACE")
        self.assertTrue(result["local_workspace_and_backups_erased"])
        self.assertFalse(self.root.exists())
        # tearDown expects the test's directory, while the erased path must stay gone.
        self.root.mkdir()

    def test_model_configuration_pinned_and_failure_never_falls_back(self):
        self.source()
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="pin")
        alternate = self.model.model_copy(update={"id": "alternate", "model": "different-fixture"})
        self.learning.models[alternate.id] = alternate
        self.learning.default_model = alternate.id
        called = []
        def failing(batch, config):
            called.append(config.model)
            raise RuntimeError("fixture provider unavailable")
        self.analyzer.analyze = failing
        result = self.learning.analyze(self.owner, run["batch_ids"][0], key="failure")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(called, ["test-double"])

    def test_committed_database_pending_anchor_recovers_and_replays(self):
        import v1.storage as storage_module
        original = storage_module.atomic_json
        triggered = []
        def crash_after_commit(path, value):
            if path.name == "anchor.json" and not triggered:
                triggered.append(True)
                raise OSError("fictional crash after DB commit")
            return original(path, value)
        with mock.patch.object(storage_module, "atomic_json", crash_after_commit):
            with self.assertRaises(OSError):
                self.source(key="source-replay")
        self.assertTrue((self.root / "pending-anchor.json").exists())
        recovered = Storage(self.root)
        self.assertTrue(recovered.verify()["valid"])
        self.assertFalse((self.root / "pending-anchor.json").exists())
        count = recovered.verify()["transactions"]
        self.source(key="source-replay")
        self.assertEqual(self.storage.verify()["transactions"], count)

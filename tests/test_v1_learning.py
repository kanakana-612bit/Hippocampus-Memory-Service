"""Only generated fictional content and newly created v1 workspaces."""
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v1.common import Conflict, Denied, Invalid, canonical
from v1.contracts import Identity, ModelConfig, SourceRegister
from v1.learning import Learning
from v1.normalization import normalize
from v1.storage import Storage


class FictionAnalyzer:
    """Deterministic test double, not a semantic-summary implementation."""
    calls = 0

    def analyze(self, batch, config):
        self.calls += 1
        event = next(e for e in batch["events"] if e["id"] in batch["write_event_ids"])
        span = {"event_id": event["id"], "version": 1, "source_digest": event["source_digest"],
                "start": 0, "end": len(event["content"]), "quote": event["content"], "role": event["role"]}
        return {"memories": [{"candidate_id": "m", "kind": "episode", "memory_type": "episodic",
                             "content": "架空の植物観察計画について話し合った。", "source_spans": [span]}],
                "contexts": [{"candidate_id": "c", "name": "架空の植物観察", "description": "架空の観察計画", "source_spans": [span]},
                             {"candidate_id": "c2", "name": "架空の科学", "description": "架空の学習", "source_spans": [span]}],
                "memberships": [{"event_id": event["id"], "context_candidate_id": "c", "membership_weight": .8, "source_spans": [span]},
                                {"event_id": event["id"], "context_candidate_id": "c2", "membership_weight": .3, "source_spans": [span]},
                                {"memory_candidate_id": "m", "context_candidate_id": "c", "membership_weight": .9, "source_spans": [span]}]}


class V1Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "tmp-v1-tests" / str(uuid.uuid4())
        self.storage = Storage.create(self.root)
        self.owner = Identity(principal_id="fiction-owner", trusted_private=True, permissions=frozenset({
            "sources.write", "learning.run", "learning.commit", "learning.review", "records.read", "time.write", "policy.write", "contexts.write", "operations.backup", "operations.erase"}))
        self.analyzer = FictionAnalyzer()
        self.model = ModelConfig(id="fiction", provider="fixture", model="test-double", context_window=65536)
        self.learning = Learning(self.storage, [self.model], self.model.id, self.analyzer)

    def tearDown(self):
        if self.storage.path.exists():
            self.assertTrue(self.storage.verify()["valid"])
        shutil.rmtree(self.root)

    def source(self, source_id="fiction-source", *, messages=None, metadata=None, scope="private", key=None):
        messages = messages or [{"role": "user", "text": "架空の庭で植物を観察したい。"}, {"role": "agent", "text": "架空の観察計画を整理しましょう。"}]
        registration = SourceRegister(id=source_id, original_name="fictional.json", data_class="synthetic", access_scope=scope)
        result = self.learning.register_source(self.owner, registration, canonical({**(metadata or {}), "messages": messages}).encode(), key=key or uuid.uuid4().hex)
        return result

    def run_learning(self, *, mode="initial", first=1, last=2, source_id="fiction-source", key=None):
        options = {"source_ids": [source_id]} if mode == "daily" else {"ranges": [{"source_id": source_id, "first": first, "last": last}]}
        run = self.learning.create_run(self.owner, mode=mode, key=key or uuid.uuid4().hex, **options)
        for batch_id in run["batch_ids"]:
            analyzed = self.learning.analyze(self.owner, batch_id, key="analyze-" + batch_id)
            self.assertEqual(analyzed["status"], "valid")
            self.learning.commit(self.owner, batch_id, key="commit-" + batch_id)
        return run

    def test_round_trip_and_independent_weights(self):
        self.source()
        run = self.run_learning()
        with self.storage.connection() as con:
            rows = list(con.execute("SELECT * FROM records"))
            self.assertEqual(len(rows), 3)
            event = next(json.loads(r["data_json"]) for r in rows if r["kind"] == "event")
            self.assertEqual(event["content"], "架空の庭で植物を観察したい。")
            memory = next(json.loads(r["data_json"]) for r in rows if r["kind"] == "episode")
            self.assertIsNone(memory["confidence"])
            self.assertEqual(memory["generalization_weight"], 0)
            self.assertEqual(con.execute("SELECT SUM(membership_weight) FROM utterance_context_membership").fetchone()[0], 1.1)
            self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT release_status FROM learning_runs").fetchone()[0], "published")
        self.learning.commit(self.owner, run["batch_ids"][0], key="commit-" + run["batch_ids"][0])
        self.assertTrue(Storage(self.root).verify()["valid"])

    def test_daily_append_stable_ids_and_old_time_late_arrival(self):
        messages = [{"role": "user", "text": "架空の庭", "datetime": "2026-09-24T12:00:00+09:00"}, {"role": "agent", "text": "架空の返答"}]
        self.source(messages=messages)
        self.run_learning()
        messages += [{"role": "user", "text": "架空の古い観察", "datetime": "2026-09-20T12:00:00+09:00"}, {"role": "agent", "text": "架空の追加返答"}]
        self.source(messages=messages)
        run = self.learning.create_run(self.owner, mode="daily", source_ids=["fiction-source"], key="daily")
        analyzed = self.learning.analyze(self.owner, run["batch_ids"][0], key="analysis")
        self.assertEqual(analyzed["status"], "review_required")
        with self.storage.connection() as con:
            staged = con.execute("SELECT * FROM staging WHERE batch_id=?", (run["batch_ids"][0],)).fetchone()
        self.learning.review(self.owner, run["batch_ids"][0], reason="架空の遅延到着を確認", expected_version=staged["version"], key="review")
        self.learning.commit(self.owner, run["batch_ids"][0], key="commit")
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM records WHERE kind='event'").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 4)
            batch = con.execute("SELECT * FROM learning_batches WHERE id=?", (run["batch_ids"][0],)).fetchone()
            self.assertEqual((batch["first_sequence"], batch["last_sequence"]), (3, 4))

    def test_gap_does_not_advance_checkpoint(self):
        self.source(messages=[{"role": "user", "text": "架空A"}, {"role": "agent", "text": "架空B"},
                              {"role": "user", "text": "架空C"}, {"role": "agent", "text": "架空D"}])
        self.run_learning(first=3, last=4)
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 0)
        self.run_learning(first=1, last=2)
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT sequence FROM learning_checkpoints").fetchone()[0], 4)

    def test_restricted_denied_before_model(self):
        self.source(scope="restricted")
        with self.assertRaises(Denied):
            self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="no")
        self.assertEqual(self.analyzer.calls, 0)

    def test_other_owner_and_guest_denied(self):
        self.source()
        for identity in (self.owner.model_copy(update={"principal_id": "other"}), self.owner.model_copy(update={"audience": "guest"})):
            with self.assertRaises(Denied):
                self.learning.create_run(identity, mode="daily", source_ids=["fiction-source"], key="denied")

    def test_invalid_span_stays_staging(self):
        self.source()
        original = self.analyzer.analyze
        def invalid(batch, config):
            value = original(batch, config)
            value["memories"][0]["source_spans"][0]["source_digest"] = "0" * 64
            return value
        self.analyzer.analyze = invalid
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="r")
        status = self.learning.analyze(self.owner, run["batch_ids"][0], key="a")
        self.assertEqual(status["status"], "failed")
        with self.assertRaises(Conflict):
            self.learning.commit(self.owner, run["batch_ids"][0], key="c")
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)

    def test_source_prefix_and_idempotency_conflict(self):
        self.source(key="source")
        with self.assertRaises(Conflict):
            self.source(key="source", messages=[{"role": "user", "text": "別の架空内容"}])
        with self.assertRaises(Conflict):
            self.source(messages=[{"role": "user", "text": "原文変更"}])

    def test_no_existing_workspace_or_unprovisioned_database_open(self):
        with self.assertRaises(FileExistsError):
            Storage.create(self.root)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        sentinel = unrelated / "existing.sqlite3"
        sentinel.write_bytes(b"do not open")
        with self.assertRaises(Invalid):
            Storage(unrelated)
        self.assertEqual(sentinel.read_bytes(), b"do not open")

    def test_run_range_rejects_partial_turn(self):
        self.source()
        with self.assertRaises(Invalid):
            self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 1}], key="partial")


class TimeCases(unittest.TestCase):
    def normalized(self, **data):
        data.setdefault("messages", [{"role": "user", "text": "架空一"}, {"role": "agent", "text": "架空二"}, {"role": "user", "text": "架空三"}])
        return normalize(canonical(data).encode(), source_id="fiction", epoch="test")

    def test_uniform_endpoints_and_n1(self):
        result = self.normalized(session_start_datetime="2026/09/24 10:00", total_session_time_minutes=30, timezone="Asia/Tokyo")
        self.assertEqual([e["time"]["relative_offset_seconds"] for e in result["events"]], [0, 900, 1800])
        self.assertEqual({e["time"]["time_quality"] for e in result["events"]}, {"estimated_uniform"})
        one = self.normalized(session_start_datetime="2026-09-24T10:00+09:00", total_session_time_minutes=30, messages=[{"role": "user", "text": "架空"}])
        self.assertEqual(one["events"][0]["time"]["relative_offset_seconds"], 0)

    def test_missing_duration_never_gets_two_minute_fallback(self):
        for missing in (None, "unknown"):
            result = self.normalized(session_start_datetime="2026/09/24 10:00", total_session_time_minutes=missing)
            self.assertEqual({e["time"]["time_quality"] for e in result["events"]}, {"unknown"})
            self.assertTrue(all(e["time"]["relative_offset_seconds"] is None for e in result["events"]))

    def test_approximate_relative_only(self):
        result = self.normalized(approximate_period="架空の夏頃")
        self.assertEqual([e["time"]["relative_offset_seconds"] for e in result["events"]], [0, 120, 240])
        self.assertTrue(all(e["time"]["event_time"] is None for e in result["events"]))

    def test_exact_precision_timezone_and_invalid_duration(self):
        for invalid in (-1, True, "30"):
            with self.assertRaises(Invalid):
                self.normalized(total_session_time_minutes=invalid)
        with self.assertRaises(Invalid):
            self.normalized(messages=[{"role": "user", "text": "架空", "datetime": "2026-09-24 10:00"}])
        exact = self.normalized(messages=[{"role": "user", "text": "架空", "datetime": "2026-09-24T10:00+09:00"}])
        self.assertEqual(exact["events"][0]["time"]["original_precision"], "minute")


if __name__ == "__main__":
    unittest.main()

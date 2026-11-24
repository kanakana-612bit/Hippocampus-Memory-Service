"""Synthetic contracts: temporal uncertainty, provider behavior and source isolation."""
import json
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock

import test_v1_learning as fixtures
from v1.analysis import ProviderAnalyzer, validate_proposal
from v1.common import Conflict, Denied, Invalid, canonical
from v1.contracts import ModelConfig, TimeInterpretation
from v1.memory import Memory
from v1.normalization import normalize


class ContractCase(TestCase):
    setUp = fixtures.V1Case.setUp
    tearDown = fixtures.V1Case.tearDown
    source = fixtures.V1Case.source
    run_learning = fixtures.V1Case.run_learning

    def test_corrected_time_is_pinned_for_reanalysis_and_stales_prior_proposal(self):
        self.source()
        self.run_learning()
        memory = Memory(self.storage)
        event = memory.timeline(self.owner, kind="event")["items"][0]
        old_run = self.learning.create_run(self.owner, mode="reanalysis", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="old")
        memory.correct_time(self.owner, event["id"], TimeInterpretation(time_quality="exact", event_time="2026-09-24T12:00:00+09:00"),
                            expected_version=1, reason="架空の根拠確認", key="correction")
        with self.assertRaises(Conflict):
            self.learning.analyze(self.owner, old_run["batch_ids"][0], key="no-stale-analysis")
        self.assertTrue(memory.timeline(self.owner, kind="episode")["items"][0]["time_review_required"])
        fresh = self.learning.create_run(self.owner, mode="reanalysis", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="fresh")
        with self.storage.connection() as con:
            batch = json.loads(con.execute("SELECT input_json FROM learning_batches WHERE id=?", (fresh["batch_ids"][0],)).fetchone()[0])
        self.assertEqual(batch["events"][0]["time"]["event_time"], "2026-09-24T12:00:00+09:00")

    def test_revoked_context_text_is_not_in_new_model_input(self):
        self.source()
        self.run_learning()
        memory = Memory(self.storage)
        with self.storage.connection() as con:
            version = con.execute("SELECT version FROM raw_sources").fetchone()[0]
        memory.set_policy(self.owner, target_kind="source", target_id="fiction-source", scope="restricted", destinations=["local"],
            expected_version=version, reason="架空の制限", key="restrict")
        self.source("different-fiction", messages=[{"role": "user", "text": "別の架空テーマ"}, {"role": "agent", "text": "別の架空応答"}])
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "different-fiction", "first": 1, "last": 2}], key="different")
        with self.storage.connection() as con:
            batch = json.loads(con.execute("SELECT input_json FROM learning_batches WHERE id=?", (run["batch_ids"][0],)).fetchone()[0])
        self.assertEqual(batch["existing_contexts"], [])

    def test_context_only_reading_cannot_create_outside_event_membership(self):
        self.source(messages=[{"role": "user", "text": "架空A"}, {"role": "agent", "text": "架空B"},
                              {"role": "user", "text": "架空C"}, {"role": "agent", "text": "架空D"}])
        self.run_learning(last=2)
        run = self.learning.create_run(self.owner, mode="daily", source_ids=["fiction-source"], key="daily")
        # Explicit read-context behavior is shared with reanalysis/initial range planning.
        overlap = self.learning.create_run(self.owner, mode="reanalysis", ranges=[{"source_id": "fiction-source", "first": 3, "last": 4,
            "context_first": 1, "context_last": 4}], key="overlap")
        with self.storage.connection() as con:
            batch = json.loads(con.execute("SELECT input_json FROM learning_batches WHERE id=?", (overlap["batch_ids"][0],)).fetchone()[0])
        proposal = self.analyzer.analyze(batch, self.model)
        proposal["memberships"][0]["event_id"] = batch["events"][0]["id"]
        with self.assertRaises(Invalid):
            validate_proposal(proposal, batch)
        self.learning.analyze(self.owner, overlap["batch_ids"][0], key="valid-overlap")
        self.learning.commit(self.owner, overlap["batch_ids"][0], key="valid-commit")
        with self.storage.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM records WHERE kind='event'").fetchone()[0], 4)

    def test_cross_owner_and_wrong_kind_membership_fk_rejected(self):
        import sqlite3
        self.source()
        self.run_learning()
        with self.storage.connection() as con:
            membership = dict(con.execute("SELECT * FROM utterance_context_membership LIMIT 1").fetchone())
            memory_id = con.execute("SELECT id FROM records WHERE kind='episode'").fetchone()[0]
        for changes in ({"principal_id": "another-fiction-owner"}, {"event_id": memory_id}, {"membership_weight": 1.1}):
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.command(self.owner, "fiction.invalid-membership", canonical(changes), changes,
                    lambda tx, changes=changes: dict(tx.insert("utterance_context_membership", {**membership, **changes, "id": "invalid-fixture"})))

    def test_recent_window_is_two_complete_turns(self):
        self.source(messages=[m for i in range(3) for m in ({"role": "user", "text": f"架空の入力{i}"}, {"role": "agent", "text": f"架空の応答{i}"})])
        self.run_learning(last=6)
        recent = Memory(self.storage).recent_turns(self.owner, "fiction-source")
        self.assertEqual(len(recent["turns"]), 2)
        self.assertEqual([e["data"]["sequence"] for turn in recent["turns"] for e in turn["events"]], [3, 4, 5, 6])

    def test_provider_protocol_strict_json_and_complete_finish(self):
        self.source()
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="provider")
        with self.storage.connection() as con:
            batch = json.loads(con.execute("SELECT input_json FROM learning_batches WHERE id=?", (run["batch_ids"][0],)).fetchone()[0])
        model = ModelConfig(id="fiction-http", provider="openai_compatible", model="fiction-provider", endpoint="http://127.0.0.1:1/v1", context_window=65536)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = canonical({"choices": [{"finish_reason": "stop", "message": {"content": '{"memories":[],"contexts":[],"memberships":[]}'}}]}).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch("v1.analysis.urllib.request.build_opener", return_value=opener):
            output = ProviderAnalyzer().analyze(batch, model)
            self.assertEqual(output["memories"], [])
            request = opener.open.call_args.args[0]
            sent = json.loads(request.data)
            self.assertEqual(sent["model"], "fiction-provider")
            self.assertIn("source_spans", sent["messages"][1]["content"])
            response.read.return_value = canonical({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}).encode()
            with self.assertRaises(Invalid):
                ProviderAnalyzer().analyze(batch, model)

    def test_salience_cannot_promote_generalization_or_memory_confidence(self):
        self.source()
        run = self.learning.create_run(self.owner, mode="initial", ranges=[{"source_id": "fiction-source", "first": 1, "last": 2}], key="weights")
        with self.storage.connection() as con:
            batch = json.loads(con.execute("SELECT input_json FROM learning_batches WHERE id=?", (run["batch_ids"][0],)).fetchone()[0])
        proposal = self.analyzer.analyze(batch, self.model)
        proposal["memories"][0]["salience"] = 1.0
        proposal["memories"][0]["retrieval_weight"] = 1.0
        valid = validate_proposal(proposal, batch)
        self.assertEqual(valid.memories[0].generalization_weight, 0)
        proposal["memories"][0]["generalization_weight"] = .9
        with self.assertRaises(Invalid):
            validate_proposal(proposal, batch)
        proposal["memories"][0]["generalization_weight"] = 0
        proposal["memories"][0]["confidence"] = 1.0
        with self.assertRaises(ValueError):
            validate_proposal(proposal, batch)


class TemporalContractCase(TestCase):
    def parse(self, messages, **metadata):
        return normalize(canonical({**metadata, "messages": messages}).encode(), source_id="fiction", epoch="test")

    def test_four_uniform_endpoints_and_equal_text_distinct_ids(self):
        messages = [{"role": "user", "text": "同一の架空文"} for _ in range(4)]
        result = self.parse(messages, session_start_datetime="2026-09-24T10:00+09:00", total_session_time_minutes=30)
        self.assertEqual([e["time"]["relative_offset_seconds"] for e in result["events"]], [0, 600, 1200, 1800])
        self.assertEqual(len({e["id"] for e in result["events"]}), 4)

    def test_dst_ambiguity_invalid_json_and_empty_messages_rejected(self):
        for raw in (b'{"messages":[],"messages":[]}', b'{"messages":[', b'{"messages":[]}'):
            with self.assertRaises(Invalid):
                normalize(raw, source_id="fiction", epoch="test")
        with self.assertRaises(Invalid):
            self.parse([{"role": "user", "text": "fiction", "datetime": "2026-11-01T01:30"}], timezone="America/New_York")

    def test_exact_millisecond_and_timezone_provenance(self):
        result = self.parse([{"role": "user", "text": "fiction", "datetime": "2026-09-24T10:00:00.123"}], timezone="Asia/Tokyo")
        temporal = result["events"][0]["time"]
        self.assertEqual(temporal["time_quality"], "exact")
        self.assertEqual(temporal["original_precision"], "fractional_3")
        self.assertEqual(temporal["timezone_source"], "original_document_timezone")
        self.assertIsNone(temporal["uncertainty_seconds"])

    def test_tool_group_preserved_and_orphan_rejected(self):
        messages = [{"role": "user", "text": "fictional lookup"},
                    {"role": "agent", "text": "", "tool_calls": [{"id": "fiction-call", "function": {"name": "fiction_tool"}}]},
                    {"role": "tool", "text": "fictional result", "tool_call_id": "fiction-call"},
                    {"role": "agent", "text": "fictional answer"}]
        normalized = self.parse(messages)
        self.assertEqual(len({e["turn_id"] for e in normalized["events"]}), 1)
        with self.assertRaises(Invalid):
            self.parse([{"role": "tool", "text": "fiction", "tool_call_id": "orphan"}])
        with self.assertRaises(Invalid):
            self.parse(messages[:2])

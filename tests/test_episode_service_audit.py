"""Episode concurrency and history regressions; fictional isolated fixtures only."""
from copy import deepcopy
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from v02.contracts import uid
from v02.episode_contracts import raw_text_digest
from v02.episode_service import EpisodeService
from v02.service import Service
from v02.storage import Conflict


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class EpisodeServiceAuditTests(unittest.TestCase):
    def setUp(self):
        self.folder = TEST_TMP / ("episode-audit-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.service = Service(self.folder / "fictional.db")
        self.episodes = EpisodeService(self.service)
        self.principal = "fictional-owner"
        self.scope = {"conversation_id": "fictional-chat"}
        self.source_text = "I decided to use a blue fictional lighthouse."
        self.serial = 0
        self.service.sync(self.principal, {"conversation_id": "fictional-chat", "messages": [
            {"id": "fictional-user", "role": "user", "content": self.source_text,
             "event_time": "2026-01-01T10:00:00+09:00"}], "active_event_ids": ["fictional-user"],
            "revision_key": "r1", "expected_revision": None, "auto_capture": False})
        self.clock = patch("v02.episode_service.now", return_value="2026-01-02T03:00:00+09:00")
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.span = {"event_ref": {"id": "fictional-user", "version": 1},
                     "source_digest": raw_text_digest(self.source_text), "start": 0,
                     "end": len(self.source_text), "quote": self.source_text}
        self.episodes.seal(self.principal, {"scope": self.scope, "path_revision": "r1",
            "idempotency_key": "fictional-seal", "reason": "fictional test"})

    def tearDown(self):
        self.assertEqual(self.folder.resolve().parent, TEST_TMP.resolve())
        shutil.rmtree(self.folder)

    def prepare(self, operation="provisional", host="fictional-host", **values):
        self.serial += 1
        return self.episodes.prepare(self.principal, host, {"scope": self.scope, "path_revision": "r1",
            "operation": operation, "idempotency_key": f"fictional-prepare-{self.serial}",
            "reason": "fictional test", **values})

    def commit(self, job, proposal, host="fictional-host"):
        self.serial += 1
        return self.episodes.commit(self.principal, host, job["job_id"], {
            "manifest_digest": job["manifest_digest"], "proposal": proposal,
            "generator": {"model_id": "fictional-model", "prompt_version": "fixture-1", "algorithm_version": "fixture-1"},
            "idempotency_key": f"fictional-commit-{self.serial}", "reason": "fictional test"})

    def proposal(self, job):
        return {"summaries": [{"chunk_ref": job["input_bundle"]["chunks"][0]["ref"],
            "summary": "The user chose a fictional blue lighthouse.", "source_spans": [self.span],
            "decision_candidates": [{"text": "Use a blue fictional lighthouse.", "source_spans": [self.span]}]}]}

    def summary(self):
        job = self.prepare()
        return self.commit(job, self.proposal(job))

    def replacement_summary(self, job, predecessor):
        value = self.proposal(job)
        value["summaries"][0]["supersedes_ref"] = predecessor
        value["summaries"][0]["summary"] = "A regenerated fictional lighthouse summary."
        return value

    def query(self):
        return self.episodes.query(self.principal, {"scope": self.scope, "path_revision": "r1",
                                                   "mode": "structured", "include_history": True})

    def test_last_committed_review_wins_with_identical_wall_clock_timestamps(self):
        candidate = self.summary()["candidate_refs"][0]
        keys = sorted(("fictional-review-" + str(i) for i in range(20)),
                      key=lambda key: uid("structured_review", self.principal, key))
        common = {"scope": self.scope, "path_revision": "r1", "target_ref": candidate, "reason": "fictional test"}
        with patch("v02.storage.now", return_value="2026-01-03T00:00:00.000+00:00"):
            confirmed = self.episodes.review(self.principal, "fictional-reviewer", {
                **common, "idempotency_key": keys[-1], "action": "confirm"})
            self.episodes.review(self.principal, "fictional-reviewer", {
                **common, "idempotency_key": keys[0], "action": "withdraw",
                "expected_review_ref": {"id": confirmed["id"], "version": confirmed["version"]}})
        self.assertEqual(self.query()["structured"][0]["adoption"], "withdrawn")

    def test_two_workers_cannot_consume_the_same_provisional_inputs(self):
        first = self.prepare(host="fictional-first-worker")
        second = self.prepare(host="fictional-second-worker")
        self.commit(first, self.proposal(first), "fictional-first-worker")
        with self.assertRaises(Conflict):
            self.commit(second, self.proposal(second), "fictional-second-worker")

    def test_explicit_new_regeneration_request_is_not_a_permanent_old_job_replay(self):
        summary = self.summary()["summary_refs"][0]
        first = self.prepare("regenerate", source_refs=[summary])
        latest = self.commit(first, self.replacement_summary(first, summary))["summary_refs"][0]
        second = self.prepare("regenerate", source_refs=[latest])
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["status"], "prepared")

    def test_summary_regeneration_requires_an_explicit_current_predecessor(self):
        summary = self.summary()["summary_refs"][0]
        regenerate = self.prepare("regenerate", source_refs=[summary])
        with self.assertRaises(ValueError):
            self.commit(regenerate, self.proposal(regenerate))
        self.assertEqual(len(self.service.list_records(self.principal, "episode_summary")), 1)

    def test_competing_summary_replacements_cannot_fork_current_state(self):
        summary = self.summary()["summary_refs"][0]
        first = self.prepare("regenerate", host="fictional-first-worker", source_refs=[summary])
        second = self.prepare("regenerate", host="fictional-second-worker", source_refs=[summary])
        self.commit(first, self.replacement_summary(first, summary), "fictional-first-worker")
        with self.assertRaises(Conflict):
            self.commit(second, self.replacement_summary(second, summary), "fictional-second-worker")
        self.assertEqual(len(self.service.list_records(self.principal, "episode_summary")), 2)

    def test_daily_inputs_use_latest_summary_without_deleting_original(self):
        original = self.summary()["summary_refs"][0]
        before = self.service.get(self.principal, original["id"])
        regenerate = self.prepare("regenerate", source_refs=[original])
        latest = self.commit(regenerate, self.replacement_summary(regenerate, original))["summary_refs"][0]
        daily = self.prepare("daily", day="2026-01-01")
        inputs = {s["ref"]["id"] for s in daily["input_bundle"]["summaries"]}
        self.assertEqual(inputs, {latest["id"]})
        self.assertEqual(self.service.get(self.principal, original["id"]), before)

    def test_superseded_summary_invalidates_current_ancestors_but_preserves_history(self):
        original = self.summary()["summary_refs"][0]
        original_record = self.service.get(self.principal, original["id"])
        daily = self.prepare("daily", day="2026-01-01")
        organized = self.commit(daily, {"episodes": [
            {"local_id": "fictional-local", "level": "local", "title": "Fictional local episode",
             "summary": "A fictional design decision.", "child_refs": [original]},
            {"local_id": "fictional-theme", "level": "theme", "title": "Fictional lighthouse project",
             "summary": "A fictional long term design theme.", "child_local_ids": ["fictional-local"]},
        ]})
        old_records = {r["id"]: self.service.get(self.principal, r["id"]) for r in organized["episode_refs"]}
        theme_id = next(identity for identity, record in old_records.items() if record["data"]["level"] == "theme")
        regenerate = self.prepare("regenerate", source_refs=[original])
        latest = self.commit(regenerate, self.replacement_summary(regenerate, original))["summary_refs"][0]
        current = self.episodes.query(self.principal, {"scope": self.scope, "path_revision": "r1", "mode": "episodic"})
        self.assertFalse({r["ref"]["id"] for r in current["episodes"]} & old_records.keys())
        history = self.episodes.query(self.principal, {"scope": self.scope, "path_revision": "r1",
                                                     "mode": "episodic", "include_history": True})
        self.assertIn(theme_id, {r["ref"]["id"] for r in history["episodes"]})
        self.assertEqual(self.service.get(self.principal, original["id"]), original_record)
        for identity, record in old_records.items():
            self.assertEqual(self.service.get(self.principal, identity), record)
        daily_rebuild = self.prepare("daily", day="2026-01-01")
        self.assertEqual({r["ref"]["id"] for r in daily_rebuild["input_bundle"]["summaries"]}, {latest["id"]})
        self.assertFalse({r["ref"]["id"] for r in daily_rebuild["input_bundle"]["existing_episodes"]} & old_records.keys())

    def test_duplicate_current_structured_topic_requires_explicit_supersession(self):
        first = self.summary()
        candidate = first["candidate_refs"][0]
        common = {"scope": self.scope, "path_revision": "r1", "reason": "fictional test"}
        confirmed = self.episodes.review(self.principal, "fictional-reviewer", {
            **common, "target_ref": candidate, "action": "confirm", "idempotency_key": "fictional-confirm-original"})
        regenerate = self.prepare("regenerate", source_refs=first["summary_refs"])
        replacement = self.commit(regenerate, self.replacement_summary(regenerate, first["summary_refs"][0]))["candidate_refs"][0]
        with self.assertRaises(Conflict):
            self.episodes.review(self.principal, "fictional-reviewer", {**common, "target_ref": replacement,
                "action": "confirm", "idempotency_key": "fictional-confirm-duplicate"})
        self.episodes.review(self.principal, "fictional-reviewer", {**common, "target_ref": candidate,
            "action": "supersede", "replacement_ref": replacement,
            "expected_review_ref": {"id": confirmed["id"], "version": confirmed["version"]},
            "idempotency_key": "fictional-supersede-original"})
        self.episodes.review(self.principal, "fictional-reviewer", {**common, "target_ref": replacement,
            "action": "confirm", "idempotency_key": "fictional-confirm-replacement"})
        history = {r["ref"]["id"]: r["adoption"] for r in self.query()["structured"]}
        self.assertEqual(history[candidate["id"]], "superseded")
        self.assertEqual(history[replacement["id"]], "current")

    def test_competing_episode_replacements_require_a_current_predecessor(self):
        summary = self.summary()["summary_refs"][0]
        daily = self.prepare("daily", day="2026-01-01")
        episode = self.commit(daily, {"episodes": [{"local_id": "fictional-local", "level": "local",
            "title": "Fictional lighthouse", "summary": "A fictional design decision.", "child_refs": [summary]}]})["episode_refs"][0]
        first = self.prepare("regenerate", host="fictional-first-worker", source_refs=[episode])
        second = self.prepare("regenerate", host="fictional-second-worker", source_refs=[episode])
        replacement = {"episodes": [{"local_id": "fictional-replacement", "level": "local",
            "title": "Fictional revised lighthouse", "summary": "A revised fictional design decision.",
            "source_spans": [self.span], "supersedes_ref": episode}]}
        self.commit(first, replacement, "fictional-first-worker")
        with self.assertRaises(Conflict):
            self.commit(second, deepcopy(replacement), "fictional-second-worker")

    def test_artifact_replacement_cannot_fork_an_already_replaced_authority(self):
        common = {"scope": self.scope, "path_revision": "r1", "title": "Fictional implementation",
                  "kind": "implementation_document", "source_spans": [self.span], "reason": "fictional test"}
        original = self.episodes.artifact(self.principal, "fictional-reviewer", {
            **common, "uri": "https://example.invalid/fictional/old", "idempotency_key": "fictional-artifact-old"})
        predecessor = {"id": original["id"], "version": original["version"]}
        self.episodes.artifact(self.principal, "fictional-reviewer", {**common,
            "uri": "https://example.invalid/fictional/new", "idempotency_key": "fictional-artifact-new",
            "supersedes_ref": predecessor})
        with self.assertRaises(Conflict):
            self.episodes.artifact(self.principal, "fictional-reviewer", {**common,
                "uri": "https://example.invalid/fictional/competing", "idempotency_key": "fictional-artifact-competing",
                "supersedes_ref": predecessor})


if __name__ == "__main__":
    unittest.main()

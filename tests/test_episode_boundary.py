"""In-flight response and compact recall boundaries with fictional dialogue only."""
from copy import deepcopy
import unittest
import test_episode_service as fixtures


class EpisodeBoundaryTests(unittest.TestCase):
    setUp = fixtures.EpisodeServiceTests.setUp
    remove_fixture = fixtures.EpisodeServiceTests.remove_fixture
    sync = fixtures.EpisodeServiceTests.sync
    command = fixtures.EpisodeServiceTests.command
    records = fixtures.EpisodeServiceTests.records
    source_span = fixtures.EpisodeServiceTests.source_span
    prepare = fixtures.EpisodeServiceTests.prepare
    commit = fixtures.EpisodeServiceTests.commit
    commit_body = fixtures.EpisodeServiceTests.commit_body
    provisional_job = fixtures.EpisodeServiceTests.provisional_job
    provisional = fixtures.EpisodeServiceTests.provisional
    daily_proposal = fixtures.EpisodeServiceTests.daily_proposal
    daily = fixtures.EpisodeServiceTests.daily
    query = fixtures.EpisodeServiceTests.query

    def pending(self):
        run = self.service.build_run(self.principal, {"conversation_id": self.scope["conversation_id"],
            "path_revision": "r1", "query": "beacon", "idempotency_key": "demo:pending-run"})
        self.service.attempt(self.principal, self.host, {"id": "demo:pending", "run_id": run["id"],
            "path_revision": "r1", "model_id": "demo:model", "packet_digest": run["data"]["packet_digest"]})
        observation = {"stage": "host_finalized", "status": "retained", "request_digest": "a" * 64,
                       "packet_digest": run["data"]["packet_digest"], "path_revision": "r1"}
        self.service.observe(self.principal, self.host, "demo:pending", observation)
        self.service.observe(self.principal, self.host, "demo:pending", {**observation, "stage": "dispatch", "status": "started"})
        return observation

    def test_pending_response_is_not_bypassed_by_caller_false(self):
        observation = self.pending()
        before = deepcopy(self.records("event"))
        held = self.ep.seal(self.principal, self.command(response_in_progress=False))
        self.assertTrue(held["response_in_progress"])
        self.assertEqual(held["chunk_refs"], [])
        self.service.observe(self.principal, self.host, "demo:pending", {**observation, "stage": "dispatch", "status": "failed"})
        self.assertEqual(len(self.ep.seal(self.principal, self.command())["chunk_refs"]), 1)
        self.assertEqual(self.records("event"), before)

    def test_older_pending_response_blocks_its_chunk_after_new_user_gap(self):
        observation = self.pending()
        event = {"id": "demo:later-user", "role": "user", "content": "Another fictional beacon topic.",
                 "event_time": "2026-09-22T12:00:00Z"}
        self.sync([event], [e["id"] for e in self.events] + [event["id"]], revision="r2")
        self.revision = "r2"
        self.assertEqual(self.ep.seal(self.principal, self.command())["chunk_refs"], [])
        self.service.observe(self.principal, self.host, "demo:pending", {**observation, "stage": "dispatch", "status": "failed"})
        self.assertEqual(len(self.ep.seal(self.principal, self.command())["chunk_refs"]), 2)

    def test_abstract_recall_keeps_addresses_without_copying_all_original_quotes(self):
        self.provisional()
        result = self.query(mode="episodic", query="")
        self.assertTrue(result["episodes"])
        self.assertTrue(result["episodes"][0]["source_spans"])
        self.assertNotIn("quote", result["episodes"][0]["source_spans"][0])
        self.assertIn("source_digest", result["episodes"][0]["source_spans"][0])
        raw = self.query(mode="raw", query="", target_ref=result["episodes"][0]["ref"])
        self.assertEqual(len(raw["sources"]), len(self.events))


if __name__ == "__main__":
    unittest.main()

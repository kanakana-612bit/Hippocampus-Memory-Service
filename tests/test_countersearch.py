"""K3 bounded actual scans over fictional canonical sources; no external services."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from itertools import chain, repeat
from threading import Event
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import test_knowledge_service as fixtures
from v02.api import Identity
from v02.countersearch_contracts import SearchExecute, SearchPlanCreate
from v02.knowledge_contracts import ClaimSnapshot
from v02.storage import Conflict


class CountersearchTests(unittest.TestCase):
    remove_fixture = fixtures.KnowledgeServiceTests.remove_fixture
    call = fixtures.KnowledgeServiceTests.call
    sync = fixtures.KnowledgeServiceTests.sync
    command = fixtures.KnowledgeServiceTests.command
    ack_ref = fixtures.KnowledgeServiceTests.ack_ref
    source = fixtures.KnowledgeServiceTests.source
    proposition = fixtures.KnowledgeServiceTests.proposition
    create_claim = fixtures.KnowledgeServiceTests.create_claim
    claim = fixtures.KnowledgeServiceTests.claim
    claim_command = fixtures.KnowledgeServiceTests.claim_command
    counts = fixtures.KnowledgeServiceTests.counts

    def setUp(self):
        fixtures.KnowledgeServiceTests.setUp(self)
        self.runner = self.service.countersearch
        clock = patch("v02.countersearch_service.now", return_value=fixtures.STAMP)
        self.wall_clock = clock.start()
        self.addCleanup(clock.stop)

    def plan_request(self, claim_id, *, queries=None, budgets=None, **changes):
        return self.claim_command(claim_id, queries=queries or [{"purpose": "falsification", "text": "blue",
            "reason": "Find potentially incompatible descriptions in the fictional observation corpus"}],
            not_applicable_reasons={"alternative_explanation": "Demo single recorded report; no mechanism asserted",
                "boundary_case": "Demo report is limited to its event", "source_correction": "Separate source-correction exercise"},
            budgets=budgets or {}, **changes)

    def plan(self, claim_id, **changes):
        result = self.runner.create_plan(self.identity, claim_id, self.plan_request(claim_id, **changes))
        ref = self.ack_ref(result, "knowledge_search_plan")
        return self.runner.get_plan(self.identity, ref["id"])

    def execute(self, plan, **changes):
        request = self.command(**changes)
        result = self.runner.execute(self.identity, plan["id"], request)
        ref = self.ack_ref(result, "knowledge_search_attempt")
        return request, result, self.runner.get_attempt(self.identity, ref["id"])

    def ready(self, claim_id, searches):
        claim = ClaimSnapshot.model_validate(self.claim(claim_id)["data"])
        with self.service.db.connection() as con:
            return self.runner.adoption_readiness(con, self.identity, claim, "r1", searches)

    def test_actual_execution_records_hits_and_frozen_corpus_without_changing_claim(self):
        claim_id, source = self.create_claim()
        before = deepcopy(self.claim(claim_id))
        plan = self.plan(claim_id)
        self.assertEqual(plan["data"]["corpus"], [{"ref": source,
            "source_digest": self.service.get(self.principal, source["id"])["data"]["source_digest"]}])
        self.assertNotIn("content", str(plan["data"]["corpus"]))
        added_after_plan = self.source("demo:tool")
        _, result, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "completed")
        self.assertEqual(attempt["data"]["result_refs"], [source])
        self.assertNotIn(added_after_plan, attempt["data"]["result_refs"])
        search = self.service.get(self.principal, self.ack_ref(result, "knowledge_search")["id"])
        self.assertEqual(search["data"]["execution_mode"], "actual_internal_corpus")
        self.assertEqual(search["data"]["snapshot"]["status"], "completed")
        self.assertEqual(search["data"]["snapshot"]["result_refs"], [source])
        self.assertEqual(self.claim(claim_id), before)
        self.assertEqual(self.service.list_records(self.principal, "knowledge_evidence"), [])
        self.assertTrue(self.service.db.verify()["valid"])

    def test_no_hit_is_only_a_completed_search_not_no_counterevidence_assessment(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id, queries=[{"purpose": "falsification", "text": "not-present-in-demo",
                                            "reason": "Demo negative control"}])
        _, _, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "completed")
        self.assertEqual(attempt["data"]["result_refs"], [])
        self.assertEqual(self.claim(claim_id)["data"]["counterevidence_status"], "unassessed")
        self.assertIsNone(self.claim(claim_id)["data"]["assessment"]["score"])

    def test_purpose_coverage_and_review_permission_cannot_be_omitted(self):
        claim_id, _ = self.create_claim()
        request = self.plan_request(claim_id)
        incomplete = deepcopy(request)
        incomplete["not_applicable_reasons"].pop("source_correction")
        with self.assertRaises(ValidationError):
            SearchPlanCreate.model_validate(incomplete)
        contradictory = deepcopy(request)
        contradictory["not_applicable_reasons"]["falsification"] = "Attempted bypass"
        with self.assertRaises(ValidationError):
            SearchPlanCreate.model_validate(contradictory)
        writer = Identity(self.principal, "demo:writer", frozenset({"read", "write"}))
        with self.assertRaises(PermissionError):
            self.runner.create_plan(writer, claim_id, request)
        plan = self.plan(claim_id)
        with self.assertRaises(PermissionError):
            self.runner.execute(writer, plan["id"], self.command())

    def test_or_terms_are_literal_casefolded_substrings_and_do_not_execute_queries(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id, queries=[{"purpose": "falsification", "text": "Demo alternative wording",
            "terms": ["NOT-IN-CORPUS", "BLUE"], "reason": "Demo OR query"}])
        _, _, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "completed")
        self.assertEqual(len(attempt["data"]["result_refs"]), 1)

    def test_query_source_result_and_eig_budgets_preserve_partial_results(self):
        claim_id, _ = self.create_claim()
        self.source("demo:tool")
        query = {"purpose": "falsification", "text": "blue", "reason": "Demo search"}
        cases = [
            ({"queries": [query, {**query, "text": "beacon"}], "budgets": {"query_limit": 1}}, "query_budget_exhausted", True),
            ({"budgets": {"source_limit": 1}}, "source_budget_exhausted", True),
            ({"budgets": {"result_limit": 1}}, "result_budget_exhausted", True),
            ({"queries": [{**query, "expected_information_gain": 0.0}]}, "low_expected_information_gain", False),
        ]
        for arguments, reason, partial in cases:
            with self.subTest(reason=reason):
                plan = self.plan(claim_id, **arguments)
                _, result, attempt = self.execute(plan)
                self.assertEqual(attempt["data"]["status"], "deferred")
                self.assertEqual(attempt["data"]["stop_reason"], reason)
                self.assertEqual(bool(attempt["data"]["result_refs"]), partial)
                search = self.service.get(self.principal, self.ack_ref(result, "knowledge_search")["id"])
                self.assertEqual(search["data"]["snapshot"]["status"], "deferred")
                self.assertIsNone(search["data"]["snapshot"]["completed_at"])

    def test_time_budget_yields_deferred_attempt_without_false_completion(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id, budgets={"time_ms": 1})
        values = chain([0.0], repeat(1.0))
        with patch.object(self.runner, "monotonic", side_effect=lambda: next(values)):
            _, _, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "deferred")
        self.assertEqual(attempt["data"]["stop_reason"], "time_budget_exhausted")
        self.assertEqual(attempt["data"]["executed_query_indices"], [])

    def test_actual_runner_has_no_write_transaction_and_stale_path_blocks_finalization(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id)
        original_run = self.runner._run

        def change_path(*args):
            # This canonical write succeeds while the search is outside its transaction.
            self.sync([], ["demo:tool"], revision="r2", expected="r1")
            return original_run(*args)

        with patch.object(self.runner, "_run", side_effect=change_path):
            _, result, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "blocked")
        self.assertEqual(attempt["data"]["stop_reason"], "path_or_target_changed")
        self.assertIsNone(attempt["data"]["search_ref"])
        self.assertEqual([item["kind"] for item in result["records"]], ["knowledge_search_attempt"])
        self.assertTrue(attempt["data"]["result_refs"])

    def test_changed_claim_revision_blocks_inflight_results_and_old_plan_execution(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id)
        original_run = self.runner._run

        def edit_claim(*args):
            proposition = self.proposition()
            proposition.update(text="The demo narrator reported red.", object="red")
            self.service.knowledge.edit_claim(self.identity, claim_id,
                self.claim_command(claim_id, proposition=proposition))
            return original_run(*args)

        with patch.object(self.runner, "_run", side_effect=edit_claim):
            _, _, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "blocked")
        self.assertIsNone(attempt["data"]["search_ref"])
        with self.assertRaises(Conflict):
            self.runner.execute(self.identity, plan["id"], self.command())

    def test_expiring_lease_prevents_completed_search_from_being_recorded(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id, lease_seconds=1)
        original_run = self.runner._run

        def expire(*args):
            result = original_run(*args)
            self.wall_clock.return_value = "2026-01-01T10:00:02Z"
            return result

        with patch.object(self.runner, "_run", side_effect=expire):
            _, _, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "blocked")
        self.assertEqual(attempt["data"]["stop_reason"], "lease_expired")
        self.assertIsNone(attempt["data"]["search_ref"])
        lease = self.service.list_records(self.principal, "knowledge_search_lease")[0]
        self.assertEqual(lease["data"]["state"], "finished")
        self.assertEqual(lease["data"]["scope"], plan["data"]["scope"])
        self.assertIsNotNone(lease["data"]["expiry"])

    def test_lease_blocks_other_hosts_and_replay_does_not_execute_again(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id)
        request = self.command()
        self.runner._acquire(self.identity, plan["id"], SearchExecute.model_validate(request))
        other = Identity(self.principal, "demo:other-host", frozenset({"review", "read"}))
        with self.assertRaises(Conflict):
            self.runner.execute(other, plan["id"], self.command())
        result = self.runner.execute(self.identity, plan["id"], request)
        before = self.counts()
        with patch.object(self.runner, "_run", side_effect=AssertionError("Replay must not run")):
            self.assertEqual(self.runner.execute(self.identity, plan["id"], request), result)
        self.assertEqual(self.counts(), before)
        with self.assertRaises(Conflict):
            self.runner.execute(other, plan["id"], request)

    def test_expired_lease_replacement_retains_old_partial_attempt_without_releasing_new_lease(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id, lease_seconds=1)
        other = Identity(self.principal, "demo:replacement-host", frozenset({"review", "read"}))
        next_request = self.command()
        original_run = self.runner._run

        def replace_lease(*args):
            result = original_run(*args)
            self.wall_clock.return_value = "2026-01-01T10:00:02Z"
            self.runner._acquire(other, plan["id"], SearchExecute.model_validate(next_request))
            return result

        with patch.object(self.runner, "_run", side_effect=replace_lease):
            _, _, first = self.execute(plan)
        self.assertEqual(first["data"]["status"], "blocked")
        self.assertEqual(first["data"]["stop_reason"], "lease_replaced")
        self.assertTrue(first["data"]["result_refs"])
        lease = self.service.list_records(self.principal, "knowledge_search_lease")[0]
        self.assertEqual(lease["data"]["host_id"], other.host)
        self.assertEqual(lease["data"]["state"], "active")
        result = self.runner.execute(other, plan["id"], next_request)
        second = self.runner.get_attempt(self.identity, self.ack_ref(result, "knowledge_search_attempt")["id"])
        self.assertEqual(second["data"]["status"], "completed")
        self.assertEqual(len(self.service.list_records(self.principal, "knowledge_search_attempt")), 2)

    def test_executor_failure_is_a_blocked_attempt_with_explicit_mode(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id)
        with patch.object(self.runner, "_run", side_effect=RuntimeError("Fictional execution error")):
            _, result, attempt = self.execute(plan)
        self.assertEqual(attempt["data"]["status"], "blocked")
        self.assertEqual(attempt["data"]["stop_reason"], "executor_error:RuntimeError")
        search = self.service.get(self.principal, self.ack_ref(result, "knowledge_search")["id"])
        self.assertEqual(search["data"]["execution_mode"], "actual_internal_corpus")
        self.assertEqual(search["data"]["snapshot"]["status"], "blocked")

    def test_concurrent_same_key_does_not_start_a_second_executor(self):
        claim_id, _ = self.create_claim()
        plan = self.plan(claim_id)
        request = self.command()
        entered, release = Event(), Event()
        original_run = self.runner._run

        def pause(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Demo executor was not released")
            return original_run(*args)

        with patch.object(self.runner, "_run", side_effect=pause) as mock_run:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.runner.execute, self.identity, plan["id"], request)
                try:
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(Conflict):
                        self.runner.execute(self.identity, plan["id"], request)
                    self.assertEqual(mock_run.call_count, 1)
                finally:
                    release.set()
                result = future.result(timeout=3)
        attempt = self.runner.get_attempt(self.identity, self.ack_ref(result, "knowledge_search_attempt")["id"])
        self.assertEqual(attempt["data"]["status"], "completed")

    def test_expired_or_replaced_running_replay_cannot_erase_original_partial_observations(self):
        claim_id, source = self.create_claim()
        plan = self.plan(claim_id, lease_seconds=1)
        old_request = self.command()
        new_request = self.command()
        other = Identity(self.principal, "demo:replacement-host", frozenset({"review", "read"}))
        entered, release = Event(), Event()
        original_run = self.runner._run
        invocation_count = 0

        def pause_first(*args):
            nonlocal invocation_count
            invocation_count += 1
            result = original_run(*args)
            if invocation_count == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Demo original worker was not released")
            return result

        with patch.object(self.runner, "_run", side_effect=pause_first):
            with ThreadPoolExecutor(max_workers=1) as pool:
                old_future = pool.submit(self.runner.execute, self.identity, plan["id"], old_request)
                try:
                    self.assertTrue(entered.wait(3))
                    self.wall_clock.return_value = "2026-01-01T10:00:02Z"
                    # Expiry alone does not let a replay finalize someone else's run.
                    with self.assertRaises(Conflict):
                        self.runner.execute(self.identity, plan["id"], old_request)
                    replacement_result = self.runner.execute(other, plan["id"], new_request)
                    replacement = self.runner.get_attempt(self.identity,
                        self.ack_ref(replacement_result, "knowledge_search_attempt")["id"])
                    self.assertEqual(replacement["data"]["status"], "completed")
                    # A replacement lease, including one already completed, grants
                    # no right to fabricate an empty old attempt either.
                    with self.assertRaises(Conflict):
                        self.runner.execute(self.identity, plan["id"], old_request)
                    self.assertEqual(len(self.service.list_records(self.principal, "knowledge_search_attempt")), 1)
                finally:
                    release.set()
                old_result = old_future.result(timeout=3)
        old = self.runner.get_attempt(self.identity, self.ack_ref(old_result, "knowledge_search_attempt")["id"])
        self.assertEqual(old["data"]["status"], "blocked")
        self.assertEqual(old["data"]["stop_reason"], "lease_replaced")
        self.assertEqual(old["data"]["result_refs"], [source])
        self.assertIsNone(old["data"]["search_ref"])
        self.assertEqual(len(self.service.list_records(self.principal, "knowledge_search_attempt")), 2)
        self.assertEqual(self.runner.execute(self.identity, plan["id"], old_request), old_result)
        self.assertEqual(invocation_count, 2)

    def test_readiness_requires_actual_selected_search_and_every_current_plan_completed(self):
        claim_id, _ = self.create_claim()
        self.assertIn("current_search_plan_required", self.ready(claim_id, []))
        plan = self.plan(claim_id)
        self.assertIn("countersearch_plan_unexecuted", self.ready(claim_id, []))
        _, result, _ = self.execute(plan)
        search = self.ack_ref(result, "knowledge_search")
        self.assertIn("actual_countersearch_required", self.ready(claim_id, []))
        self.assertEqual(self.ready(claim_id, [search]), ())
        pending = self.plan(claim_id, budgets={"time_ms": 1})
        self.assertIn("countersearch_plan_unexecuted", self.ready(claim_id, [search]))
        with patch.object(self.runner, "monotonic", side_effect=chain([0.0], repeat(1.0))):
            self.execute(pending)
        self.assertIn("countersearch_plan_incomplete", self.ready(claim_id, [search]))


if __name__ == "__main__":
    unittest.main()

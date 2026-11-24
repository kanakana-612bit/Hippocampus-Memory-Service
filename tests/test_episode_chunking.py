"""Fictional clock/path examples; no model, database or runtime files."""
import unittest

from v02.episode_chunking import plan_chunks
from v02.episode_contracts import ChunkPolicy


def event(identity, at, role="user", content="fictional"):
    return {"id": identity, "role": role, "event_time": f"2026-09-23T{at}:00Z", "content": content}


def plan(events, at, **kwargs):
    return plan_chunks(events, [e["id"] for e in events], now=f"2026-09-23T{at}:00Z", **kwargs)


class EpisodeChunkingTests(unittest.TestCase):
    def test_given_example_variable_length_and_next_start(self):
        events = [event("a", "10:00"), event("b", "10:05"), event("c", "10:18"), event("d", "10:42")]
        self.assertEqual(plan(events, "11:11").sealed, ())
        closed = plan(events, "11:12").sealed
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].event_ids, ("a", "b", "c", "d"))
        self.assertIn("10:42", closed[0].ended_at)
        events.extend([event("e", "13:20"), event("f", "13:27")])
        followup = plan(events, "13:57", already_sealed_event_ids=("a", "b", "c", "d"))
        self.assertEqual(followup.sealed[0].event_ids, ("e", "f"))

    def test_assistant_and_tool_output_do_not_reset_user_timer(self):
        events = [event("a", "10:00"), event("b", "10:20", "tool"), event("c", "10:25", "assistant")]
        closed = plan(events, "10:30").sealed
        self.assertEqual(closed[0].event_ids, ("a", "b", "c"))
        self.assertIn("10:25", closed[0].ended_at)
        self.assertIn("10:30", closed[0].eligible_at)

    def test_active_response_delays_trailing_seal_even_after_deadline(self):
        events = [event("a", "10:00"), event("b", "10:31", "assistant")]
        active = plan(events, "10:35", response_in_progress=True)
        self.assertEqual(active.sealed, ())
        self.assertEqual(active.pending_event_ids, ("a", "b"))
        done = plan(events, "10:35")
        self.assertEqual(done.sealed[0].event_ids, ("a", "b"))
        self.assertIn("10:31", done.sealed[0].eligible_at)

    def test_historical_gaps_close_while_latest_response_is_active(self):
        events = [event("a", "10:00"), event("b", "10:01", "assistant"),
                  event("c", "11:00"), event("d", "11:01", "assistant")]
        result = plan(events, "11:35", response_in_progress=True)
        self.assertEqual(result.sealed[0].event_ids, ("a", "b"))
        self.assertEqual(result.pending_event_ids, ("c", "d"))

    def test_exact_threshold_is_idle_not_fixed_windows(self):
        events = [event("a", "10:00"), event("b", "10:29"), event("c", "10:58")]
        self.assertEqual(plan(events, "11:00").sealed, ())
        self.assertEqual(len(plan(events, "11:28").sealed), 1)
        events = [event("a", "10:00"), event("b", "10:30")]
        result = plan(events, "10:31")
        self.assertEqual(result.sealed[0].event_ids, ("a",))
        self.assertEqual(result.pending_event_ids, ("b",))

    def test_midnight_is_not_a_boundary(self):
        events = [event("a", "23:50"), {**event("b", "00:10"), "event_time": "2026-09-24T00:10:00Z"}]
        result = plan_chunks(events, ["a", "b"], now="2026-09-24T00:40:00Z")
        self.assertEqual(result.sealed[0].event_ids, ("a", "b"))

    def test_size_limits_split_between_turns_with_explicit_processing_reason(self):
        events = [event("a", "10:00"), event("b", "10:01", "assistant"),
                  event("c", "10:02"), event("d", "10:03", "assistant")]
        result = plan(events, "10:05", policy=ChunkPolicy(max_events=2))
        self.assertEqual(result.sealed[0].reason, "processing_limit")
        self.assertEqual(result.sealed[0].event_ids, ("a", "b"))
        self.assertEqual(result.pending_event_ids, ("c", "d"))

    def test_large_single_turn_keeps_full_source(self):
        events = [event("a", "10:00", content="f" * 50), event("b", "10:01", "assistant", "d" * 50)]
        result = plan(events, "10:05", policy=ChunkPolicy(max_characters=10), response_in_progress=True)
        self.assertEqual(result.sealed, ())
        self.assertEqual(result.pending_event_ids, ("a", "b"))
        self.assertIn("oversized_turn_retained", result.warnings)

    def test_invalid_event_time_uses_receipt_not_wall_clock_guess(self):
        events = [{**event("a", "10:00"), "event_time": "broken", "received_at": "2026-09-23T10:00:00Z"}]
        result = plan(events, "10:30")
        self.assertEqual(len(result.sealed), 1)
        self.assertIn("receipt_time_fallback:a:received_at", result.warnings)
        events[0].pop("received_at")
        with self.assertRaises(ValueError):
            plan(events, "10:30")

    def test_canonical_record_created_at_fallback(self):
        record = {"id": "a", "version": 1, "created_at": "2026-09-23T10:00:00Z",
                  "data": {"role": "user", "content": "Fictional", "event_time": None}}
        result = plan_chunks([record], ["a"], now="2026-09-23T10:30:00Z")
        self.assertEqual(result.sealed[0].event_ids, ("a",))
        self.assertIn("receipt_time_fallback:a:created_at", result.warnings)

    def test_clock_skew_never_reorders_path(self):
        events = [event("a", "10:10"), event("b", "10:05"), event("c", "10:11", "assistant")]
        result = plan(events, "10:40")
        self.assertEqual(result.sealed[0].event_ids, ("a", "b", "c"))
        self.assertIn("nonmonotonic_time_clamped:b", result.warnings)

    def test_future_event_does_not_prematurely_close(self):
        result = plan([event("a", "11:00")], "10:30")
        self.assertEqual(result.sealed, ())
        self.assertIn("future_event_time:a", result.warnings)

    def test_prefix_holes_and_missing_duplicate_events_rejected(self):
        events = [event("a", "10:00"), event("b", "10:01")]
        with self.assertRaises(ValueError):
            plan(events, "10:31", already_sealed_event_ids=["b"])
        for path in (["a", "a"], ["a", "missing"]):
            with self.assertRaises(ValueError):
                plan_chunks(events, path, now="2026-09-23T10:31:00Z")
        with self.assertRaises(ValueError):
            plan_chunks([events[0], events[0]], ["a"], now="2026-09-23T10:31:00Z")

    def test_no_user_has_no_inactivity_timer(self):
        result = plan([event("system", "10:00", "system")], "12:00")
        self.assertEqual(result.sealed, ())
        self.assertEqual(result.pending_event_ids, ("system",))

    def test_empty_path_and_all_sealed_are_noops(self):
        self.assertEqual(plan([], "10:00").pending_event_ids, ())
        result = plan([event("a", "10:00")], "10:30", already_sealed_event_ids=["a"])
        self.assertEqual(result.sealed, ())
        self.assertEqual(result.pending_event_ids, ())


if __name__ == "__main__":
    unittest.main()

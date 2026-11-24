"""Fictional derived-memory contracts; no deployment data is accessed."""
from copy import deepcopy
import unittest

from pydantic import ValidationError

from v02.episode_contracts import (
    ArtifactReferenceCreate, ChunkPolicy, EpisodeProposal, JobCommit, JobPrepare,
    SourceSpan, StructuredCandidate, StructuredReview, raw_text_digest,
)


def span(text="Demo user chose a blue fictional beacon."):
    return {"event_ref": {"id": "demo:event", "version": 1},
            "source_digest": raw_text_digest(text), "start": 0, "end": len(text), "quote": text}


def episode(identity="local", children=()):
    return {"local_id": identity, "level": "local", "title": "Demo choice",
            "summary": "A fictional beacon choice was discussed.",
            "source_spans": [span()], "child_local_ids": list(children)}


def command():
    return {"idempotency_key": "demo:command", "reason": "Fictional contract fixture",
            "scope": {"conversation_id": "demo:conversation"}, "path_revision": "demo:path"}


class EpisodeContractTests(unittest.TestCase):
    def test_unicode_offsets_count_codepoints_not_utf16(self):
        text = "空🌟白"
        value = span(text)
        value.update(start=1, end=2, quote="🌟")
        self.assertEqual(SourceSpan.model_validate(value).quote, "🌟")
        value["end"] = 3
        with self.assertRaises(ValidationError):
            SourceSpan.model_validate(value)

    def test_raw_digest_never_normalizes(self):
        self.assertNotEqual(raw_text_digest("x\r\ny"), raw_text_digest("x\ny"))
        self.assertNotEqual(raw_text_digest("é"), raw_text_digest("e\u0301"))

    def test_spans_require_exact_nonempty_ranges(self):
        for changes in ({"start": -1}, {"end": 0}, {"start": True}, {"source_digest": "fake"},
                        {"event_ref": {"id": "demo:event", "version": 0}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                SourceSpan.model_validate({**span(), **changes})

    def test_overlapping_sources_and_multi_parent_dag_are_valid(self):
        proposal = EpisodeProposal.model_validate({"episodes": [
            episode("leaf"), episode("left", ["leaf"]), episode("right", ["leaf"]),
            episode("root", ["left", "right"]),
        ]})
        self.assertEqual(len(proposal.episodes), 4)

    def test_hierarchy_rejects_cycle_unknown_child_duplicate_ids(self):
        cases = ([episode("a", ["b"]), episode("b", ["a"])],
                 [episode("a", ["missing"])], [episode("a"), episode("a")])
        for episodes in cases:
            with self.subTest(episodes=episodes), self.assertRaises(ValidationError):
                EpisodeProposal.model_validate({"episodes": episodes})

    def test_hierarchy_requires_raw_or_derived_grounding(self):
        value = episode()
        value["source_spans"] = []
        with self.assertRaises(ValidationError):
            EpisodeProposal.model_validate({"episodes": [value]})

    def test_candidate_cannot_assert_adoption_confidence_or_weight(self):
        candidate = {"kind": "user_decision", "subject": "demo:user", "topic": "demo choice",
                     "text": "The demo user chose blue.", "source_spans": [span()]}
        self.assertEqual(StructuredCandidate.model_validate(candidate).status, "candidate")
        for change in ({"status": "confirmed"}, {"confidence": 1.0}, {"salience": 1.0},
                       {"generalization_weight": 1.0}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                StructuredCandidate.model_validate({**candidate, **change})

    def test_candidate_local_episode_must_exist(self):
        candidate = {"kind": "user_intent", "subject": "demo:user", "topic": "demo plan",
                     "text": "The demo user might test blue.", "source_spans": [span()],
                     "episode_local_ids": ["missing"]}
        with self.assertRaises(ValidationError):
            EpisodeProposal.model_validate({"structured_candidates": [candidate]})

    def test_provisional_summary_has_explicit_empty_extraction_categories(self):
        proposal = EpisodeProposal.model_validate({"summaries": [{
            "chunk_ref": {"id": "demo:chunk", "version": 1},
            "summary": "Fictional conversation.", "source_spans": [span()],
        }]})
        summary = proposal.model_dump()["summaries"][0]
        for key in ("topics", "events", "decision_candidates", "stance_candidates",
                    "intent_candidates", "unresolved_candidates", "related_entities"):
            self.assertEqual(summary[key], ())

    def test_summary_items_need_own_spans(self):
        with self.assertRaises(ValidationError):
            EpisodeProposal.model_validate({"summaries": [{
                "chunk_ref": {"id": "demo:chunk", "version": 1},
                "summary": "Fictional summary", "source_spans": [span()],
                "events": [{"text": "Ungrounded event", "source_spans": []}],
            }]})

    def test_prepare_calendar_validation_and_explicit_regeneration(self):
        self.assertEqual(JobPrepare.model_validate({**command(), "operation": "daily", "day": "2026-09-23"}).day,
                         "2026-09-23")
        for values in ({"operation": "daily", "day": "2026-02-30"},
                       {"operation": "daily", "day": "20260923"},
                       {"operation": "regenerate"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                JobPrepare.model_validate({**command(), **values})

    def test_commit_does_not_accept_caller_confidence_or_clock(self):
        commit = {"idempotency_key": "demo:commit", "reason": "Fictional result",
                  "manifest_digest": "a" * 64, "proposal": {}, "generator": {
                      "model_id": "demo:model", "prompt_version": "demo:v1", "algorithm_version": "demo:v1"}}
        JobCommit.model_validate(commit)
        with self.assertRaises(ValidationError):
            JobCommit.model_validate({**commit, "now": "2026-09-23T03:00:00Z"})
        with self.assertRaises(ValidationError):
            JobCommit.model_validate({**commit, "confidence": 1.0})

    def test_review_cas_and_supersession_contract(self):
        req = {**command(), "target_ref": {"id": "demo:old", "version": 1}, "action": "confirm",
               "expected_review_ref": {"id": "demo:review", "version": 1}}
        self.assertEqual(StructuredReview.model_validate(req).expected_review_ref.id, "demo:review")
        with self.assertRaises(ValidationError):
            StructuredReview.model_validate({**req, "action": "supersede"})
        with self.assertRaises(ValidationError):
            StructuredReview.model_validate({**req, "action": "supersede", "replacement_ref": req["target_ref"]})
        with self.assertRaises(ValidationError):
            StructuredReview.model_validate({**req, "action": "withdraw", "unresolved_status": "resolved"})

    def test_artifact_is_pointer_without_credentials(self):
        req = {**command(), "title": "Fictional design", "uri": "https://example.invalid/design",
               "kind": "design_document", "source_spans": [span()]}
        ArtifactReferenceCreate.model_validate(req)
        with self.assertRaises(ValidationError):
            ArtifactReferenceCreate.model_validate({**req, "uri": "https://user:password@example.invalid/design"})

    def test_chunk_limits_are_strict_positive_processing_settings(self):
        self.assertEqual(ChunkPolicy().inactivity_seconds, 1800)
        for values in ({"inactivity_seconds": 0}, {"max_events": True}, {"max_characters": -1}):
            with self.assertRaises(ValidationError):
                ChunkPolicy.model_validate(values)


if __name__ == "__main__":
    unittest.main()

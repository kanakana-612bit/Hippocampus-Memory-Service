"""Fictional context-isolation regressions across the existing knowledge paths."""
from pathlib import Path
import os
import shutil
import unittest
from unittest.mock import patch
import uuid

from v02.api import Identity
from v02.contracts import digest, provenance
from v02.knowledge_contracts import KnowledgeCandidate, KnowledgeScope, initial_claim
from v02.service import Service


TMP = Path(__file__).resolve().parents[1] / "tmp-tests"


class EpisodeKnowledgeContextTests(unittest.TestCase):
    def setUp(self):
        self.folder = TMP / ("episode-context-knowledge-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_fixture)
        env = patch.dict(os.environ, {"HIPPOCAMPUS_V02_KEY_PASSPHRASE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.service = Service(self.folder / "demo.db", knowledge_mode="enabled")
        self.principal = "demo:owner"
        self.identity = Identity(self.principal, "demo:host", frozenset({"read", "write", "review"}))
        self.scope = {"conversation_id": "demo:chat", "context_key": "conversation", "sandbox_id": None}
        self.foreign_text = "Fictional beacon detail in a different context."
        self.service.sync(self.principal, {"conversation_id": "demo:chat", "revision_key": "r1",
            "expected_revision": None, "auto_capture": False, "active_event_ids": ["demo:local", "demo:foreign"],
            "messages": [{"id": "demo:local", "role": "user", "content": "Fictional local beacon.",
                          "event_time": "2026-09-22T10:00:00Z"},
                         {"id": "demo:foreign", "role": "user", "content": self.foreign_text,
                          "context_key": "demo:separate-context", "event_time": "2026-09-22T10:01:00Z"}]})

    def remove_fixture(self):
        target = self.folder.resolve()
        self.assertEqual(target.parent, TMP.resolve())
        self.assertTrue(target.name.startswith("episode-context-knowledge-"))
        if target.exists():
            shutil.rmtree(target)

    def command(self, key, **values):
        return {"idempotency_key": "demo:" + key, "reason": "Fictional scope isolation",
                "path_revision": "r1", "scope": self.scope, **values}

    def count(self):
        with self.service.db.connection() as con:
            return con.execute("SELECT count(*) FROM records").fetchone()[0]

    def legacy_mislabeled_source(self):
        """Simulate a source allowed by the pre-context guard, without real data."""
        scope = KnowledgeScope(principal_id=self.principal, **self.scope)
        reference = {"id": "demo:legacy-source", "version": 1}
        with self.service.db.connection(write=True) as con:
            self.service.db.put(con, identity=reference["id"], principal=self.principal, kind="knowledge_source",
                conversation="demo:chat", reason="fictional_pre_context_source", data={
                    "scope": scope.model_dump(), "event_ref": {"id": "demo:foreign", "version": 1},
                    "parent_source_refs": [], "event_refs": [{"id": "demo:foreign", "version": 1}],
                    "origin_ids": ["demo:foreign"], "content": self.foreign_text,
                    "source_digest": digest(self.foreign_text), "role": "user", "actor_id": "demo:user",
                    "event_time": "2026-09-22T10:01:00Z", "provenance": provenance("demo:fixture", "fictional history")})
        return reference, scope

    def test_source_registration_rejects_foreign_context_atomically(self):
        before = self.count()
        with self.assertRaises(ValueError):
            self.service.knowledge.create_source(self.identity,
                self.command("source", event_ref={"id": "demo:foreign", "version": 1}))
        self.assertEqual(self.count(), before)

    def test_source_registration_accepts_matching_explicit_context(self):
        result = self.service.knowledge.create_source(self.identity, self.command("source",
            scope={**self.scope, "context_key": "demo:separate-context"},
            event_ref={"id": "demo:foreign", "version": 1}))
        self.assertEqual(result["records"][0]["kind"], "knowledge_source")

    def test_extraction_rechecks_raw_closure_not_only_source_scope_label(self):
        source, _ = self.legacy_mislabeled_source()
        proposal = {"proposition": {"text": self.foreign_text, "kind": "attributed_report",
                                    "attribution_actor_id": "demo:user"},
                    "spans": [{"source_ref": source, "start": 0, "end": len(self.foreign_text), "quote": self.foreign_text}],
                    "reason": "Fictional attempted extraction"}
        before = self.count()
        with self.assertRaises(ValueError):
            self.service.extraction.submit(self.identity, self.command("extract", proposals=[proposal]))
        self.assertEqual(self.count(), before)

    def test_legacy_mislabeled_claim_is_not_injected_as_a_caveat(self):
        source, scope = self.legacy_mislabeled_source()
        claim = initial_claim(KnowledgeCandidate(id="demo:legacy-claim", scope=scope,
            proposition={"text": self.foreign_text, "kind": "attributed_report", "attribution_actor_id": "demo:user"},
            origin_refs=[source], provenance=provenance("demo:fixture", "fictional pre-context claim")))
        with self.service.db.connection(write=True) as con:
            self.service.db.put(con, identity=claim.id, principal=self.principal, kind="knowledge_claim",
                conversation="demo:chat", data=claim.model_dump(), reason="fictional_pre_context_claim")
        with self.service.db.connection() as con:
            selected = self.service.knowledge_retrieval.select(con, self.principal, "demo:chat", "r1", "beacon")
        self.assertEqual(selected["dictionary_claims"], [])
        self.assertEqual(selected["knowledge_caveats"], [])
        self.assertEqual(selected["selected"], [])


if __name__ == "__main__":
    unittest.main()

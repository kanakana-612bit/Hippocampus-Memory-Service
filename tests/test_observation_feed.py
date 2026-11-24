"""Fictional observation fixtures. Never opens a deployment database."""
import hashlib
import json
import tempfile
import shutil
import uuid
import unittest
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from v02.api import create_app
from v02.observation import changes, export, prune
from v02.service import Service


class ObservationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "tmp-tests"
        root.mkdir(exist_ok=True)
        self.folder = root / ("observation-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.service = Service(self.folder / "fixture.db")
        self.db = self.service.db
        self.principal = "fictional-owner"

    def tearDown(self):
        self.assertEqual(self.folder.resolve().parent, (Path(__file__).resolve().parents[1] / "tmp-tests").resolve())
        shutil.rmtree(self.folder)

    def put(self, uid, principal=None, conversation="fictional-chat", version=None):
        with self.db.connection(write=True) as con:
            return self.db.put(con, identity=uid, principal=principal or self.principal, kind="unresolved", conversation=conversation,
                               data={"description": "fictional unresolved item", "status": "active"}, reason="fixture", expected=version)

    def test_export_changes_are_read_only_and_keep_after_images(self):
        initial = export(self.db, self.principal)
        self.put("item")
        self.put("item", version=1)
        batch = changes(self.db, self.principal, initial["cursor"])
        self.assertEqual([c["record"]["version"] for c in batch["changes"]], [1, 2])
        before = self.db.verify()["audit"]["event_count"]
        for _ in range(3):
            export(self.db, self.principal)
            changes(self.db, self.principal, batch["cursor"])
        self.assertEqual(self.db.verify()["audit"]["event_count"], before)

    def test_transaction_page_is_atomic_and_rollback_is_invisible(self):
        cursor = export(self.db, self.principal)["cursor"]
        with self.db.connection(write=True) as con:
            for uid in ("a", "b", "c"):
                self.db.put(con, identity=uid, principal=self.principal, kind="unresolved", conversation="fictional-chat",
                            data={"status": "active"}, reason="fixture")
        batch = changes(self.db, self.principal, cursor, limit=1)
        self.assertEqual(len(batch["changes"]), 3)
        self.assertEqual(len({c["transaction_id"] for c in batch["changes"]}), 1)
        with self.assertRaises(RuntimeError):
            with self.db.connection(write=True) as con:
                self.db.put(con, identity="rolledback", principal=self.principal, kind="unresolved", conversation="fictional-chat", data={}, reason="fixture")
                raise RuntimeError("rollback")
        self.assertEqual(changes(self.db, self.principal, batch["cursor"])["changes"], [])

    def test_principal_conversation_and_cursor_scope(self):
        self.put("mine")
        self.put("someone-else", "other-owner")
        self.put("other-conversation", conversation="other-chat")
        result = export(self.db, self.principal, "fictional-chat")
        self.assertEqual([r["id"] for r in result["records"]], ["mine"])
        for principal, conversation in (("other-owner", "fictional-chat"), (self.principal, None)):
            with self.assertRaises(HTTPException):
                changes(self.db, principal, result["cursor"], conversation)

    def test_deletion_tombstone_removes_old_observation_content(self):
        self.put("a")
        cursor = export(self.db, self.principal)["cursor"]
        with self.db.connection(write=True) as con:
            con.execute("DELETE FROM records WHERE id='a'")
        batch = changes(self.db, self.principal, cursor)
        self.assertEqual(batch["changes"][0]["operation"], "delete")
        with self.db.connection() as con:
            self.assertNotIn("fictional unresolved item", str(list(con.execute("SELECT record_json FROM observation_outbox WHERE object_id='a'"))))

    def test_unauthenticated_and_write_permissions_do_not_grant_read(self):
        credentials = [{"token_sha256": hashlib.sha256(b"fixture-write-token").hexdigest(), "principal_id": self.principal, "host_id": "fixture", "permissions": ["write"]}]
        with TestClient(create_app(self.service, credentials)) as client:
            self.assertEqual(client.get("/api/v0.2/observation/export").status_code, 401)
            self.assertEqual(client.get("/api/v0.2/observation/export", headers={"Authorization": "Bearer fixture-write-token"}).status_code, 403)

    def test_retention_expires_cursor_without_deleting_canonical_records(self):
        initial = export(self.db, self.principal)
        self.put("retained")
        with self.db.connection(write=True) as con:
            con.execute("UPDATE observation_outbox SET recorded_at=0")
        self.assertEqual(prune(self.db), 1)
        with self.assertRaises(HTTPException):
            changes(self.db, self.principal, initial["cursor"])
        current = export(self.db, self.principal)
        self.assertEqual(len(current["records"]), 1)
        self.assertEqual(changes(self.db, self.principal, current["cursor"])["changes"], [])
        self.put("new")
        self.assertEqual(changes(self.db, self.principal, current["cursor"])["changes"][0]["record"]["id"], "new")


if __name__ == "__main__":
    unittest.main()

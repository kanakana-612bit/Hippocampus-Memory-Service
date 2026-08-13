from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path

from memory_manager import MemoryManager


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "tmp-tests"


class Phase5SecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TMP / f"phase5-{uuid.uuid4().hex}.db"
        self.manager = MemoryManager(self.db_path)
        self.security_dir = self.manager.layered.security.security_dir
        self.backup_dir = self.manager.layered.backups.backup_dir

    def tearDown(self) -> None:
        for path in TEST_TMP.glob(f"{self.db_path.stem}*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        shutil.rmtree(self.security_dir, ignore_errors=True)

    def _ingest(self, suffix: str) -> None:
        self.manager.ingest_messages(
            "phase5-public-test",
            [
                {
                    "id": f"phase5-source-{suffix}",
                    "role": "user",
                    "content": f"Fictional public checkpoint datum {suffix}",
                }
            ],
            auto_capture=False,
        )

    def test_initial_checkpoint_and_external_anchor_are_valid(self) -> None:
        status = self.manager.phase5_status()

        self.assertTrue(status["complete"])
        self.assertEqual(status["verification"]["checkpoint_count"], 1)
        self.assertTrue(status["verification"]["anchor_valid"])
        self.assertTrue(status["identity"]["signing_available"])
        self.assertTrue(self.manager.layered.security.anchor_path.is_file())

    def test_new_checkpoint_covers_the_current_audit_head(self) -> None:
        self._ingest("checkpoint")
        result = self.manager.create_signed_checkpoint("test_checkpoint")
        verification = self.manager.verify_checkpoints()

        self.assertEqual(result["checkpoint"]["event_count"], 1)
        self.assertTrue(result["anchor"]["valid"])
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["uncheckpointed_event_count"], 0)

    def test_external_anchor_detects_whole_database_rollback(self) -> None:
        old_snapshot = TEST_TMP / f"{self.db_path.stem}-old.db"
        source = sqlite3.connect(self.db_path)
        target = sqlite3.connect(old_snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        self._ingest("new-tail")
        self.manager.create_signed_checkpoint("new_tail")

        shutil.copy2(old_snapshot, self.db_path)
        rolled_back = MemoryManager(self.db_path)
        verification = rolled_back.verify_checkpoints()

        self.assertFalse(verification["valid"])
        self.assertTrue(verification["rollback_detected"])
        self.assertFalse(rolled_back.phase5_status()["complete"])

    def test_key_rotation_is_dual_signed_and_tampering_is_detected(self) -> None:
        before = self.manager.phase5_status()["identity"]["active_key_id"]
        rotation = self.manager.rotate_signing_key()
        after = self.manager.phase5_status()["identity"]["active_key_id"]

        self.assertNotEqual(before, after)
        self.assertEqual(rotation["new_key_id"], after)
        self.assertTrue(self.manager.verify_checkpoints()["valid"])

        with self.manager.connect() as con:
            con.execute("DROP TRIGGER key_rotations_no_update")
            con.execute(
                "UPDATE key_rotations SET old_signature_b64='invalid' WHERE rotation_id=?",
                (rotation["rotation_id"],),
            )
        verification = self.manager.verify_checkpoints()
        self.assertFalse(verification["valid"])
        self.assertFalse(verification["key_chain_valid"])

    def test_signed_backup_detects_file_changes(self) -> None:
        self._ingest("backup")
        backup = self.manager.create_signed_backup("public-test")
        verified = self.manager.verify_signed_backup(backup["filename"])

        self.assertTrue(verified["valid"])
        backup_path = self.backup_dir / backup["filename"]
        with backup_path.open("ab") as handle:
            handle.write(b"changed")
        changed = self.manager.verify_signed_backup(backup["filename"])
        self.assertFalse(changed["valid"])
        self.assertIn("database_digest_mismatch", changed["reasons"])

    def test_old_backup_restore_is_recorded_as_a_new_branch(self) -> None:
        self._ingest("before-backup")
        backup = self.manager.create_signed_backup("restore-source")
        self._ingest("after-backup")
        latest = self.manager.create_signed_checkpoint("after_backup")
        previous_anchor = json.loads(
            self.manager.layered.security.anchor_path.read_text(encoding="utf-8")
        )
        plan = self.manager.plan_backup_restore(backup["filename"])

        self.assertEqual(plan["relation"], "rollback_to_ancestor")
        self.assertTrue(plan["requires_new_branch"])
        restore = self.manager.layered.backups.restore_offline(
            backup["filename"], previous_anchor=previous_anchor
        )
        restored = MemoryManager(self.db_path)
        self.assertTrue(restored.verify_checkpoints()["rollback_detected"])

        branch = restored.adopt_restore_branch(previous_anchor)
        status = restored.phase5_status()
        branches = restored.list_audit_branches()

        self.assertTrue(status["complete"])
        self.assertEqual(len(branches), 2)
        self.assertEqual(
            branch["previous_canonical_checkpoint_id"],
            latest["checkpoint"]["checkpoint_id"],
        )
        self.assertEqual(branches[-1]["branch_id"], branch["branch_id"])
        recovery_path = self.db_path.with_name(restore["recovery_filename"])
        self.assertTrue(recovery_path.is_file())


if __name__ == "__main__":
    unittest.main()

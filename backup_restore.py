from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit_ledger import AuditLedger
from checkpoint_security import CheckpointSecurity


BACKUP_FORMAT = "hippocampus-backup-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class BackupRestore:
    def __init__(
        self,
        db_path: str | Path,
        security: CheckpointSecurity,
        audit: AuditLedger | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.security = security
        self.audit = audit or AuditLedger()
        self.backup_dir = Path(
            os.getenv(
                "HIPPOCAMPUS_BACKUP_DIR", str(self.security.security_dir / "backups")
            )
        )

    def _backup_path(self, filename: str) -> Path:
        if Path(filename).name != filename or not filename.endswith(".db"):
            raise ValueError("Backup filename must be a plain .db filename")
        candidate = (self.backup_dir / filename).resolve()
        root = self.backup_dir.resolve()
        if candidate.parent != root:
            raise ValueError("Backup path escapes the configured backup directory")
        return candidate

    @staticmethod
    def _manifest_path(backup_path: Path) -> Path:
        return backup_path.with_name(f"{backup_path.name}.manifest.json")

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def create(
        self,
        *,
        checkpoint: dict[str, Any],
        anchor: dict[str, Any],
        label: str | None = None,
    ) -> dict[str, Any]:
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label or "manual").strip("-")
        safe_label = (safe_label or "manual")[:40]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_id = f"backup_{uuid.uuid4().hex}"
        filename = f"hippocampus-{safe_label}-{stamp}.db"
        target = self._backup_path(filename)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        source = self._connect(self.db_path)
        destination = self._connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.replace(temporary, target)

        payload = {
            "format": BACKUP_FORMAT,
            "backup_id": backup_id,
            "filename": filename,
            "db_sha256": file_sha256(target),
            "size_bytes": target.stat().st_size,
            "checkpoint_id": checkpoint["checkpoint_id"],
            "checkpoint_hash": checkpoint["checkpoint_hash"],
            "branch_id": checkpoint["branch_id"],
            "event_count": checkpoint["event_count"],
            "created_at": utc_now(),
        }
        con = self._connect(self.db_path)
        try:
            signed = self.security.sign_document(con, payload)
        finally:
            con.close()
        manifest = {"signed_manifest": signed, "anchor": anchor}
        manifest_path = self._manifest_path(target)
        manifest_temp = manifest_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        manifest_temp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_temp, manifest_path)
        return {
            "backup_id": backup_id,
            "filename": filename,
            "manifest_filename": manifest_path.name,
            "size_bytes": payload["size_bytes"],
            "db_sha256": payload["db_sha256"],
            "checkpoint_id": payload["checkpoint_id"],
            "checkpoint_hash": payload["checkpoint_hash"],
            "branch_id": payload["branch_id"],
            "created_at": payload["created_at"],
        }

    def verify(self, filename: str) -> dict[str, Any]:
        target = self._backup_path(filename)
        manifest_path = self._manifest_path(target)
        reasons: list[str] = []
        if not target.is_file():
            return {"valid": False, "filename": filename, "reasons": ["backup_missing"]}
        if not manifest_path.is_file():
            return {"valid": False, "filename": filename, "reasons": ["manifest_missing"]}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"valid": False, "filename": filename, "reasons": ["manifest_invalid"]}
        signed = manifest.get("signed_manifest")
        anchor = manifest.get("anchor")
        if not isinstance(signed, dict) or not isinstance(anchor, dict):
            return {
                "valid": False,
                "filename": filename,
                "reasons": ["manifest_structure_invalid"],
            }
        signature = self.security.verify_document(signed, anchor)
        if not signature["valid"]:
            reasons.append("manifest_signature_invalid")
        payload = signed.get("payload") if isinstance(signed.get("payload"), dict) else {}
        if payload.get("format") != BACKUP_FORMAT:
            reasons.append("backup_format_invalid")
        if payload.get("filename") != filename:
            reasons.append("filename_mismatch")
        actual_hash = file_sha256(target)
        if payload.get("db_sha256") != actual_hash:
            reasons.append("database_digest_mismatch")
        actual_size = target.stat().st_size
        if payload.get("size_bytes") != actual_size:
            reasons.append("database_size_mismatch")
        anchor_checkpoint = anchor.get("checkpoint", {})
        if (
            payload.get("checkpoint_id") != anchor_checkpoint.get("checkpoint_id")
            or payload.get("checkpoint_hash") != anchor_checkpoint.get("checkpoint_hash")
        ):
            reasons.append("manifest_anchor_mismatch")

        integrity_check = "unavailable"
        audit_verification: dict[str, Any] = {"valid": False}
        checkpoint_verification: dict[str, Any] = {"valid": False}
        checkpoint_ids: set[str] = set()
        try:
            con = self._connect(target)
            try:
                integrity_check = str(con.execute("PRAGMA integrity_check").fetchone()[0])
                audit_verification = self.audit.verify(con)
                checkpoint_verification = self.security.verify_checkpoints(
                    con, anchor=anchor, use_configured_anchor=False
                )
                checkpoint_ids = {
                    str(row["checkpoint_id"])
                    for row in con.execute("SELECT checkpoint_id FROM audit_checkpoints")
                }
            finally:
                con.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            reasons.append(f"database_open_failed:{type(exc).__name__}")
        if integrity_check != "ok":
            reasons.append("sqlite_integrity_failed")
        if not audit_verification.get("valid"):
            reasons.append("audit_verification_failed")
        if not checkpoint_verification.get("valid"):
            reasons.append("checkpoint_verification_failed")
        if payload.get("checkpoint_id") not in checkpoint_ids:
            reasons.append("manifest_checkpoint_missing")

        return {
            "valid": not reasons,
            "filename": filename,
            "manifest_filename": manifest_path.name,
            "backup_id": payload.get("backup_id"),
            "checkpoint_id": payload.get("checkpoint_id"),
            "checkpoint_hash": payload.get("checkpoint_hash"),
            "branch_id": payload.get("branch_id"),
            "event_count": payload.get("event_count"),
            "created_at": payload.get("created_at"),
            "size_bytes": actual_size,
            "db_sha256": actual_hash,
            "sqlite_integrity": integrity_check,
            "manifest_signature": signature,
            "audit": audit_verification,
            "checkpoints": checkpoint_verification,
            "reasons": reasons,
        }

    def plan_restore(self, filename: str, con: sqlite3.Connection) -> dict[str, Any]:
        verification = self.verify(filename)
        if not verification["valid"]:
            return {
                "valid": False,
                "filename": filename,
                "action": "reject",
                "verification": verification,
            }
        current = self.security.latest_checkpoint(con)
        if current is None:
            raise RuntimeError("Current database has no signed checkpoint")
        backup_id = verification["checkpoint_id"]
        backup_hash = verification["checkpoint_hash"]
        if current["checkpoint_id"] == backup_id and current["checkpoint_hash"] == backup_hash:
            relation = "same_checkpoint"
            action = "replace_equivalent"
            requires_branch = False
        else:
            backup_is_ancestor = con.execute(
                "SELECT 1 FROM audit_checkpoints WHERE checkpoint_id=? AND checkpoint_hash=?",
                (backup_id, backup_hash),
            ).fetchone() is not None
            target = self._backup_path(filename)
            backup_con = self._connect(target)
            try:
                current_is_ancestor = backup_con.execute(
                    "SELECT 1 FROM audit_checkpoints WHERE checkpoint_id=? AND checkpoint_hash=?",
                    (current["checkpoint_id"], current["checkpoint_hash"]),
                ).fetchone() is not None
            finally:
                backup_con.close()
            if backup_is_ancestor:
                relation = "rollback_to_ancestor"
            elif current_is_ancestor:
                relation = "fast_forward_to_descendant"
            else:
                relation = "divergent_lineage"
            action = "restore_as_new_branch"
            requires_branch = True
        current_count = int(con.execute("SELECT count(*) FROM audit_events").fetchone()[0])
        backup_count = int(verification.get("event_count") or 0)
        return {
            "valid": True,
            "filename": filename,
            "action": action,
            "relation": relation,
            "requires_new_branch": requires_branch,
            "current_checkpoint_id": current["checkpoint_id"],
            "current_checkpoint_hash": current["checkpoint_hash"],
            "backup_checkpoint_id": backup_id,
            "backup_checkpoint_hash": backup_hash,
            "current_event_count": current_count,
            "backup_event_count": backup_count,
            "possibly_lost_event_count": max(0, current_count - backup_count),
            "verification": verification,
            "apply_mode": "offline_only",
        }

    def restore_offline(
        self,
        filename: str,
        *,
        previous_anchor: dict[str, Any],
    ) -> dict[str, Any]:
        target = self._backup_path(filename)
        verification = self.verify(filename)
        if not verification["valid"]:
            raise ValueError("Backup verification failed")
        anchor_verification = self.security.verify_anchor_document(previous_anchor)
        if not anchor_verification["valid"]:
            raise ValueError("Current external anchor is invalid")

        recovery = self.db_path.with_name(
            f"{self.db_path.stem}-pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
        )
        temporary = self.db_path.with_suffix(f".{uuid.uuid4().hex}.restore.tmp")
        shutil.copy2(self.db_path, recovery)
        shutil.copy2(target, temporary)
        os.replace(temporary, self.db_path)
        return {
            "restored": True,
            "filename": filename,
            "recovery_filename": recovery.name,
            "previous_anchor": previous_anchor,
        }

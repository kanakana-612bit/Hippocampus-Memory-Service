from __future__ import annotations

import base64
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from audit_ledger import GENESIS_HASH, AuditLedger, canonical_json, sha256_text


CHECKPOINT_FORMAT = "hippocampus-checkpoint-v1"
ANCHOR_FORMAT = "hippocampus-anchor-v1"
ROTATION_FORMAT = "hippocampus-key-rotation-v1"
KEY_ALGORITHM = "Ed25519"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _key_id(public_bytes: bytes) -> str:
    return f"ed25519:{sha256_text(public_bytes.hex())[:24]}"


class CheckpointSecurity:
    def __init__(self, db_path: str | Path, audit: AuditLedger | None = None) -> None:
        self.db_path = Path(db_path)
        default_root = self.db_path.parent / f".{self.db_path.stem}-security"
        self.security_dir = Path(
            os.getenv("HIPPOCAMPUS_SECURITY_DIR", str(default_root))
        )
        self.key_dir = self.security_dir / "keys"
        self.anchor_path = Path(
            os.getenv(
                "HIPPOCAMPUS_ANCHOR_PATH",
                str(self.security_dir / "anchors" / "latest-checkpoint.json"),
            )
        )
        self.passphrase = os.getenv("HIPPOCAMPUS_KEY_PASSPHRASE")
        self.audit = audit or AuditLedger()

    def migrate_schema(self, con: sqlite3.Connection) -> None:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS signing_keys (
                key_id TEXT PRIMARY KEY,
                algorithm TEXT NOT NULL,
                public_key_b64 TEXT NOT NULL,
                predecessor_key_id TEXT,
                trust_origin TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS key_rotations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                rotation_id TEXT NOT NULL UNIQUE,
                old_key_id TEXT NOT NULL,
                new_key_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                old_signature_b64 TEXT NOT NULL,
                new_signature_b64 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_branches (
                branch_id TEXT PRIMARY KEY,
                parent_branch_id TEXT,
                fork_checkpoint_id TEXT,
                fork_checkpoint_hash TEXT,
                previous_canonical_checkpoint_id TEXT,
                previous_canonical_checkpoint_hash TEXT,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_branch_adoptions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                adoption_id TEXT NOT NULL UNIQUE,
                branch_id TEXT NOT NULL,
                previous_branch_id TEXT,
                reason TEXT NOT NULL,
                adopted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_checkpoints (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                checkpoint_id TEXT NOT NULL UNIQUE,
                branch_id TEXT NOT NULL,
                sequence_end INTEGER NOT NULL,
                head_event_id TEXT,
                head_event_hash TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                previous_checkpoint_id TEXT,
                previous_checkpoint_hash TEXT,
                key_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                signature_b64 TEXT NOT NULL,
                checkpoint_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_branch
                ON audit_checkpoints(branch_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_head
                ON audit_checkpoints(head_event_hash, sequence_end);
            CREATE INDEX IF NOT EXISTS idx_key_rotations_keys
                ON key_rotations(old_key_id, new_key_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_branch_adoptions_branch
                ON audit_branch_adoptions(branch_id, sequence);
            """
        )
        for table in (
            "signing_keys",
            "key_rotations",
            "audit_branches",
            "audit_branch_adoptions",
            "audit_checkpoints",
        ):
            con.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END;
                """
            )

    def ensure_identity(self, con: sqlite3.Connection) -> dict[str, Any]:
        key_count = int(con.execute("SELECT count(*) FROM signing_keys").fetchone()[0])
        if key_count == 0:
            key = self._generate_key()
            con.execute(
                """
                INSERT INTO signing_keys (
                    key_id, algorithm, public_key_b64, predecessor_key_id,
                    trust_origin, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    key["key_id"],
                    KEY_ALGORITHM,
                    key["public_key_b64"],
                    None,
                    "local_initial",
                    key["created_at"],
                ),
            )
        branch_count = int(con.execute("SELECT count(*) FROM audit_branches").fetchone()[0])
        if branch_count == 0:
            created_at = utc_now()
            branch_id = f"branch_{uuid.uuid4().hex}"
            con.execute(
                """
                INSERT INTO audit_branches (
                    branch_id, parent_branch_id, fork_checkpoint_id,
                    fork_checkpoint_hash, previous_canonical_checkpoint_id,
                    previous_canonical_checkpoint_hash, reason, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    branch_id,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "phase5_initial_lineage",
                    created_at,
                ),
            )
            con.execute(
                """
                INSERT INTO audit_branch_adoptions (
                    adoption_id, branch_id, previous_branch_id, reason, adopted_at
                ) VALUES (?,?,?,?,?)
                """,
                (
                    f"adopt_{uuid.uuid4().hex}",
                    branch_id,
                    None,
                    "phase5_initial_lineage",
                    created_at,
                ),
            )
        return self.identity_status(con)

    def _generate_key(self, predecessor_key_id: str | None = None) -> dict[str, str]:
        private_key = Ed25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        key_id = _key_id(public_bytes)
        encryption: serialization.KeySerializationEncryption
        if self.passphrase:
            encryption = serialization.BestAvailableEncryption(
                self.passphrase.encode("utf-8")
            )
        else:
            encryption = serialization.NoEncryption()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
        self.key_dir.mkdir(parents=True, exist_ok=True)
        target = self.key_dir / f"{key_id.replace(':', '_')}.pem"
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(private_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "key_id": key_id,
            "public_key_b64": _b64(public_bytes),
            "predecessor_key_id": predecessor_key_id or "",
            "created_at": utc_now(),
        }

    def _private_key_path(self, key_id: str) -> Path:
        return self.key_dir / f"{key_id.replace(':', '_')}.pem"

    def _load_private_key(self, key_id: str) -> Ed25519PrivateKey:
        path = self._private_key_path(key_id)
        if not path.is_file():
            raise RuntimeError(f"Private signing key is unavailable: {key_id}")
        password = self.passphrase.encode("utf-8") if self.passphrase else None
        key = serialization.load_pem_private_key(path.read_bytes(), password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError(f"Unsupported private key type: {key_id}")
        return key

    def _sign(self, key_id: str, value: str) -> str:
        return _b64(self._load_private_key(key_id).sign(value.encode("utf-8")))

    def sign_document(
        self, con: sqlite3.Connection, payload: dict[str, Any]
    ) -> dict[str, Any]:
        key = self.active_key(con)
        payload_json = canonical_json(payload)
        return {
            "payload": payload,
            "payload_digest": sha256_text(payload_json),
            "key_id": key["key_id"],
            "algorithm": KEY_ALGORITHM,
            "signature_b64": self._sign(key["key_id"], payload_json),
        }

    def verify_document(
        self, document: dict[str, Any], anchor: dict[str, Any]
    ) -> dict[str, Any]:
        anchor_verification = self.verify_anchor_document(anchor)
        reasons: list[str] = []
        payload = document.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            reasons.append("payload_missing")
        payload_json = canonical_json(payload)
        if document.get("payload_digest") != sha256_text(payload_json):
            reasons.append("payload_digest_mismatch")
        key_map = {
            str(key.get("key_id")): key
            for key in anchor.get("keys", [])
            if isinstance(key, dict)
        }
        key = key_map.get(str(document.get("key_id")))
        if document.get("algorithm") != KEY_ALGORITHM:
            reasons.append("algorithm_invalid")
        if key is None or not self._verify_signature(
            key["public_key_b64"], payload_json, str(document.get("signature_b64"))
        ):
            reasons.append("signature_invalid")
        if not anchor_verification["valid"]:
            reasons.append("anchor_invalid")
        return {
            "valid": not reasons,
            "reasons": reasons,
            "anchor": anchor_verification,
        }

    @staticmethod
    def _verify_signature(public_key_b64: str, value: str, signature_b64: str) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64)).verify(
                _unb64(signature_b64), value.encode("utf-8")
            )
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    @staticmethod
    def _public_key_matches_id(key_id: str, public_key_b64: str) -> bool:
        try:
            return _key_id(_unb64(public_key_b64)) == key_id
        except (ValueError, TypeError):
            return False

    def active_key(self, con: sqlite3.Connection) -> sqlite3.Row:
        rotated = con.execute(
            """
            SELECT key.* FROM key_rotations AS rotation
            JOIN signing_keys AS key ON key.key_id=rotation.new_key_id
            ORDER BY rotation.sequence DESC LIMIT 1
            """
        ).fetchone()
        if rotated is not None:
            return rotated
        initial = con.execute(
            "SELECT * FROM signing_keys WHERE predecessor_key_id IS NULL ORDER BY created_at, key_id LIMIT 1"
        ).fetchone()
        if initial is None:
            raise RuntimeError("No signing identity is configured")
        return initial

    def active_branch(self, con: sqlite3.Connection) -> sqlite3.Row:
        row = con.execute(
            """
            SELECT branch.* FROM audit_branch_adoptions AS adoption
            JOIN audit_branches AS branch ON branch.branch_id=adoption.branch_id
            ORDER BY adoption.sequence DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("No active audit branch is configured")
        return row

    def identity_status(self, con: sqlite3.Connection) -> dict[str, Any]:
        active = self.active_key(con)
        return {
            "algorithm": active["algorithm"],
            "active_key_id": active["key_id"],
            "signing_available": self._private_key_path(active["key_id"]).is_file(),
            "private_key_protection": "passphrase_and_filesystem"
            if self.passphrase
            else "filesystem_only",
            "key_count": int(con.execute("SELECT count(*) FROM signing_keys").fetchone()[0]),
            "rotation_count": int(con.execute("SELECT count(*) FROM key_rotations").fetchone()[0]),
        }

    def latest_checkpoint(
        self, con: sqlite3.Connection, branch_id: str | None = None
    ) -> sqlite3.Row | None:
        branch_id = branch_id or self.active_branch(con)["branch_id"]
        return con.execute(
            "SELECT * FROM audit_checkpoints WHERE branch_id=? ORDER BY sequence DESC LIMIT 1",
            (branch_id,),
        ).fetchone()

    @staticmethod
    def _checkpoint_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def create_checkpoint(
        self,
        con: sqlite3.Connection,
        *,
        reason: str = "manual",
        key_id: str | None = None,
    ) -> dict[str, Any]:
        audit_verification = self.audit.verify(con)
        if not audit_verification["valid"]:
            raise RuntimeError("Audit verification failed; refusing to sign a checkpoint")
        branch = self.active_branch(con)
        key = (
            con.execute("SELECT * FROM signing_keys WHERE key_id=?", (key_id,)).fetchone()
            if key_id
            else self.active_key(con)
        )
        if key is None:
            raise KeyError(f"Unknown signing key: {key_id}")
        previous = self.latest_checkpoint(con, branch["branch_id"])
        if previous is None and branch["fork_checkpoint_id"]:
            previous = con.execute(
                "SELECT * FROM audit_checkpoints WHERE checkpoint_id=?",
                (branch["fork_checkpoint_id"],),
            ).fetchone()

        head = con.execute(
            "SELECT sequence, event_id, event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        created_at = utc_now()
        checkpoint_id = f"checkpoint_{uuid.uuid4().hex}"
        payload = {
            "format": CHECKPOINT_FORMAT,
            "checkpoint_id": checkpoint_id,
            "branch_id": branch["branch_id"],
            "sequence_end": int(head["sequence"]) if head else 0,
            "head_event_id": head["event_id"] if head else None,
            "head_event_hash": head["event_hash"] if head else GENESIS_HASH,
            "event_count": int(audit_verification["event_count"]),
            "previous_checkpoint_id": previous["checkpoint_id"] if previous else None,
            "previous_checkpoint_hash": previous["checkpoint_hash"] if previous else None,
            "key_id": key["key_id"],
            "reason": reason,
            "created_at": created_at,
        }
        payload_json = canonical_json(payload)
        payload_digest = sha256_text(payload_json)
        signature_b64 = self._sign(key["key_id"], payload_json)
        checkpoint_hash = sha256_text(
            canonical_json(
                {
                    "format": CHECKPOINT_FORMAT,
                    "payload_digest": payload_digest,
                    "signature_b64": signature_b64,
                }
            )
        )
        con.execute(
            """
            INSERT INTO audit_checkpoints (
                checkpoint_id, branch_id, sequence_end, head_event_id,
                head_event_hash, event_count, previous_checkpoint_id,
                previous_checkpoint_hash, key_id, payload_json, payload_digest,
                signature_b64, checkpoint_hash, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                checkpoint_id,
                payload["branch_id"],
                payload["sequence_end"],
                payload["head_event_id"],
                payload["head_event_hash"],
                payload["event_count"],
                payload["previous_checkpoint_id"],
                payload["previous_checkpoint_hash"],
                payload["key_id"],
                payload_json,
                payload_digest,
                signature_b64,
                checkpoint_hash,
                created_at,
            ),
        )
        row = con.execute(
            "SELECT * FROM audit_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)
        ).fetchone()
        return self._checkpoint_record(row)

    def rotate_key(self, con: sqlite3.Connection) -> dict[str, Any]:
        current = self.active_key(con)
        generated = self._generate_key(predecessor_key_id=current["key_id"])
        rotation_id = f"rotation_{uuid.uuid4().hex}"
        created_at = generated["created_at"]
        payload = {
            "format": ROTATION_FORMAT,
            "rotation_id": rotation_id,
            "old_key_id": current["key_id"],
            "new_key_id": generated["key_id"],
            "new_public_key_b64": generated["public_key_b64"],
            "created_at": created_at,
        }
        payload_json = canonical_json(payload)
        con.execute(
            """
            INSERT INTO signing_keys (
                key_id, algorithm, public_key_b64, predecessor_key_id,
                trust_origin, created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                generated["key_id"],
                KEY_ALGORITHM,
                generated["public_key_b64"],
                current["key_id"],
                "dual_signed_rotation",
                created_at,
            ),
        )
        con.execute(
            """
            INSERT INTO key_rotations (
                rotation_id, old_key_id, new_key_id, payload_json,
                payload_digest, old_signature_b64, new_signature_b64, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                rotation_id,
                current["key_id"],
                generated["key_id"],
                payload_json,
                sha256_text(payload_json),
                self._sign(current["key_id"], payload_json),
                self._sign(generated["key_id"], payload_json),
                created_at,
            ),
        )
        checkpoint = self.create_checkpoint(
            con, reason="key_rotation", key_id=generated["key_id"]
        )
        return {
            "rotation_id": rotation_id,
            "old_key_id": current["key_id"],
            "new_key_id": generated["key_id"],
            "checkpoint": checkpoint,
        }

    def list_keys(self, con: sqlite3.Connection) -> list[dict[str, Any]]:
        active_id = self.active_key(con)["key_id"]
        return [
            {
                **dict(row),
                "active": row["key_id"] == active_id,
                "private_key_available": self._private_key_path(row["key_id"]).is_file(),
            }
            for row in con.execute(
                "SELECT * FROM signing_keys ORDER BY created_at, key_id"
            ).fetchall()
        ]

    def list_branches(self, con: sqlite3.Connection) -> list[dict[str, Any]]:
        active_id = self.active_branch(con)["branch_id"]
        result: list[dict[str, Any]] = []
        rows = con.execute("SELECT * FROM audit_branches ORDER BY created_at, branch_id").fetchall()
        for row in rows:
            item = dict(row)
            item["active"] = row["branch_id"] == active_id
            item["checkpoint_count"] = int(
                con.execute(
                    "SELECT count(*) FROM audit_checkpoints WHERE branch_id=?",
                    (row["branch_id"],),
                ).fetchone()[0]
            )
            result.append(item)
        return result

    def list_checkpoints(self, con: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
        rows = con.execute(
            "SELECT * FROM audit_checkpoints ORDER BY sequence DESC LIMIT ?",
            (max(1, min(1000, int(limit))),),
        ).fetchall()
        return [self._checkpoint_record(row) for row in rows]

    @staticmethod
    def _key_chain_data(con: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        keys = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM signing_keys ORDER BY created_at, key_id"
            ).fetchall()
        ]
        rotations = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM key_rotations ORDER BY sequence"
            ).fetchall()
        ]
        return keys, rotations

    def anchor_document(
        self, con: sqlite3.Connection, checkpoint_id: str | None = None
    ) -> dict[str, Any]:
        if checkpoint_id:
            checkpoint = con.execute(
                "SELECT * FROM audit_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)
            ).fetchone()
        else:
            checkpoint = self.latest_checkpoint(con)
        if checkpoint is None:
            raise RuntimeError("No checkpoint is available to anchor")
        keys, rotations = self._key_chain_data(con)
        return {
            "format": ANCHOR_FORMAT,
            "checkpoint": self._checkpoint_record(checkpoint),
            "keys": keys,
            "rotations": rotations,
            "exported_at": utc_now(),
        }

    def write_anchor(self, anchor: dict[str, Any]) -> Path:
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.anchor_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(anchor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.anchor_path)
        return self.anchor_path

    def read_anchor(self) -> dict[str, Any] | None:
        if not self.anchor_path.is_file():
            return None
        try:
            value = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"format": "invalid"}
        return value if isinstance(value, dict) else {"format": "invalid"}

    def _verify_key_chain(
        self, keys: list[dict[str, Any]], rotations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        key_map = {str(key.get("key_id")): key for key in keys}
        if len(key_map) != len(keys):
            errors.append({"reason": "duplicate_key_id"})
        roots = [key for key in keys if not key.get("predecessor_key_id")]
        if len(roots) != 1:
            errors.append({"reason": "expected_single_trust_root", "count": len(roots)})
        for key in keys:
            if key.get("algorithm") != KEY_ALGORITHM:
                errors.append({"key_id": key.get("key_id"), "reason": "unsupported_algorithm"})
            if not self._public_key_matches_id(
                str(key.get("key_id")), str(key.get("public_key_b64"))
            ):
                errors.append({"key_id": key.get("key_id"), "reason": "key_id_mismatch"})

        active_id = str(roots[0]["key_id"]) if len(roots) == 1 else None
        rotated_ids: set[str] = set()
        for rotation in rotations:
            reasons: list[str] = []
            old = key_map.get(str(rotation.get("old_key_id")))
            new = key_map.get(str(rotation.get("new_key_id")))
            payload_json = str(rotation.get("payload_json") or "")
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {}
                reasons.append("invalid_payload_json")
            if rotation.get("payload_digest") != sha256_text(payload_json):
                reasons.append("payload_digest_mismatch")
            expected = {
                "format": ROTATION_FORMAT,
                "rotation_id": rotation.get("rotation_id"),
                "old_key_id": rotation.get("old_key_id"),
                "new_key_id": rotation.get("new_key_id"),
                "new_public_key_b64": new.get("public_key_b64") if new else None,
                "created_at": rotation.get("created_at"),
            }
            if payload != expected:
                reasons.append("rotation_payload_mismatch")
            if old is None or not self._verify_signature(
                old["public_key_b64"], payload_json, str(rotation.get("old_signature_b64"))
            ):
                reasons.append("old_signature_invalid")
            if new is None or not self._verify_signature(
                new["public_key_b64"], payload_json, str(rotation.get("new_signature_b64"))
            ):
                reasons.append("new_signature_invalid")
            if new and new.get("predecessor_key_id") != rotation.get("old_key_id"):
                reasons.append("predecessor_mismatch")
            if active_id != rotation.get("old_key_id"):
                reasons.append("rotation_order_mismatch")
            if reasons:
                errors.append(
                    {"rotation_id": rotation.get("rotation_id"), "reasons": reasons}
                )
            else:
                active_id = str(rotation["new_key_id"])
                rotated_ids.add(active_id)
        for key in keys:
            if key.get("predecessor_key_id") and key.get("key_id") not in rotated_ids:
                errors.append({"key_id": key.get("key_id"), "reason": "unproven_rotated_key"})
        return {
            "valid": not errors,
            "active_key_id": active_id,
            "key_count": len(keys),
            "rotation_count": len(rotations),
            "errors": errors,
            "key_map": key_map,
        }

    def _verify_checkpoint_record(
        self,
        checkpoint: dict[str, Any],
        key_map: dict[str, dict[str, Any]],
    ) -> list[str]:
        reasons: list[str] = []
        payload = checkpoint.get("payload")
        if payload is None:
            try:
                payload = json.loads(str(checkpoint.get("payload_json") or ""))
            except json.JSONDecodeError:
                payload = {}
                reasons.append("invalid_payload_json")
        payload_json = canonical_json(payload)
        if checkpoint.get("payload_digest") != sha256_text(payload_json):
            reasons.append("payload_digest_mismatch")
        expected = {
            "format": CHECKPOINT_FORMAT,
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "branch_id": checkpoint.get("branch_id"),
            "sequence_end": checkpoint.get("sequence_end"),
            "head_event_id": checkpoint.get("head_event_id"),
            "head_event_hash": checkpoint.get("head_event_hash"),
            "event_count": checkpoint.get("event_count"),
            "previous_checkpoint_id": checkpoint.get("previous_checkpoint_id"),
            "previous_checkpoint_hash": checkpoint.get("previous_checkpoint_hash"),
            "key_id": checkpoint.get("key_id"),
            "reason": payload.get("reason") if isinstance(payload, dict) else None,
            "created_at": checkpoint.get("created_at"),
        }
        if payload != expected:
            reasons.append("checkpoint_payload_mismatch")
        key = key_map.get(str(checkpoint.get("key_id")))
        if key is None or not self._verify_signature(
            key["public_key_b64"], payload_json, str(checkpoint.get("signature_b64"))
        ):
            reasons.append("signature_invalid")
        expected_hash = sha256_text(
            canonical_json(
                {
                    "format": CHECKPOINT_FORMAT,
                    "payload_digest": checkpoint.get("payload_digest"),
                    "signature_b64": checkpoint.get("signature_b64"),
                }
            )
        )
        if checkpoint.get("checkpoint_hash") != expected_hash:
            reasons.append("checkpoint_hash_mismatch")
        return reasons

    def verify_anchor_document(self, anchor: dict[str, Any] | None) -> dict[str, Any]:
        if not anchor:
            return {"valid": False, "present": False, "errors": ["anchor_missing"]}
        if anchor.get("format") != ANCHOR_FORMAT:
            return {"valid": False, "present": True, "errors": ["anchor_format_invalid"]}
        keys = anchor.get("keys") if isinstance(anchor.get("keys"), list) else []
        rotations = (
            anchor.get("rotations") if isinstance(anchor.get("rotations"), list) else []
        )
        key_verification = self._verify_key_chain(keys, rotations)
        checkpoint = anchor.get("checkpoint")
        checkpoint_errors = (
            self._verify_checkpoint_record(checkpoint, key_verification["key_map"])
            if isinstance(checkpoint, dict)
            else ["checkpoint_missing"]
        )
        return {
            "valid": key_verification["valid"] and not checkpoint_errors,
            "present": True,
            "checkpoint_id": checkpoint.get("checkpoint_id")
            if isinstance(checkpoint, dict)
            else None,
            "checkpoint_hash": checkpoint.get("checkpoint_hash")
            if isinstance(checkpoint, dict)
            else None,
            "key_chain_valid": key_verification["valid"],
            "checkpoint_signature_valid": not checkpoint_errors,
            "key_errors": key_verification["errors"],
            "checkpoint_errors": checkpoint_errors,
            "errors": [],
        }

    def verify_checkpoints(
        self,
        con: sqlite3.Connection,
        *,
        anchor: dict[str, Any] | None = None,
        use_configured_anchor: bool = True,
    ) -> dict[str, Any]:
        keys, rotations = self._key_chain_data(con)
        key_verification = self._verify_key_chain(keys, rotations)
        rows = con.execute("SELECT * FROM audit_checkpoints ORDER BY sequence").fetchall()
        checkpoints = [self._checkpoint_record(row) for row in rows]
        checkpoint_map = {item["checkpoint_id"]: item for item in checkpoints}
        branches = {
            str(row["branch_id"])
            for row in con.execute("SELECT branch_id FROM audit_branches").fetchall()
        }
        checkpoint_errors: list[dict[str, Any]] = []
        for checkpoint in checkpoints:
            reasons = self._verify_checkpoint_record(checkpoint, key_verification["key_map"])
            if checkpoint["branch_id"] not in branches:
                reasons.append("branch_missing")
            previous_id = checkpoint.get("previous_checkpoint_id")
            previous_hash = checkpoint.get("previous_checkpoint_hash")
            if bool(previous_id) != bool(previous_hash):
                reasons.append("incomplete_previous_checkpoint_reference")
            elif previous_id:
                previous = checkpoint_map.get(str(previous_id))
                if previous is None:
                    reasons.append("previous_checkpoint_missing")
                elif previous["checkpoint_hash"] != previous_hash:
                    reasons.append("previous_checkpoint_hash_mismatch")
            sequence_end = int(checkpoint["sequence_end"])
            event_count = int(checkpoint["event_count"])
            if sequence_end == 0:
                if (
                    event_count != 0
                    or checkpoint["head_event_id"] is not None
                    or checkpoint["head_event_hash"] != GENESIS_HASH
                ):
                    reasons.append("genesis_boundary_mismatch")
            else:
                event = con.execute(
                    "SELECT event_id, event_hash FROM audit_events WHERE sequence=?",
                    (sequence_end,),
                ).fetchone()
                count = int(
                    con.execute(
                        "SELECT count(*) FROM audit_events WHERE sequence<=?", (sequence_end,)
                    ).fetchone()[0]
                )
                if event is None:
                    reasons.append("checkpointed_event_missing")
                else:
                    if event["event_id"] != checkpoint["head_event_id"]:
                        reasons.append("head_event_id_mismatch")
                    if event["event_hash"] != checkpoint["head_event_hash"]:
                        reasons.append("head_event_hash_mismatch")
                if count != event_count:
                    reasons.append("event_count_mismatch")
            if reasons:
                checkpoint_errors.append(
                    {"checkpoint_id": checkpoint["checkpoint_id"], "reasons": reasons}
                )

        if anchor is None and use_configured_anchor:
            anchor = self.read_anchor()
        anchor_verification = self.verify_anchor_document(anchor)
        anchor_checkpoint_present = False
        rollback_detected = False
        if anchor_verification["valid"] and anchor:
            anchored = anchor["checkpoint"]
            local = checkpoint_map.get(str(anchored["checkpoint_id"]))
            anchor_checkpoint_present = bool(
                local and local["checkpoint_hash"] == anchored["checkpoint_hash"]
            )
            rollback_detected = not anchor_checkpoint_present

        active_branch = self.active_branch(con)
        latest = self.latest_checkpoint(con, active_branch["branch_id"])
        event_count = int(con.execute("SELECT count(*) FROM audit_events").fetchone()[0])
        latest_count = int(latest["event_count"]) if latest else 0
        return {
            "valid": (
                key_verification["valid"]
                and not checkpoint_errors
                and bool(checkpoints)
                and anchor_verification["valid"]
                and not rollback_detected
            ),
            "key_chain_valid": key_verification["valid"],
            "checkpoint_chain_valid": not checkpoint_errors and bool(checkpoints),
            "anchor_valid": anchor_verification["valid"],
            "anchor_checkpoint_present": anchor_checkpoint_present,
            "rollback_detected": rollback_detected,
            "checkpoint_count": len(checkpoints),
            "active_branch_id": active_branch["branch_id"],
            "latest_checkpoint_id": latest["checkpoint_id"] if latest else None,
            "latest_checkpoint_hash": latest["checkpoint_hash"] if latest else None,
            "checkpointed_event_count": latest_count,
            "uncheckpointed_event_count": max(0, event_count - latest_count),
            "key_errors": key_verification["errors"],
            "checkpoint_errors": checkpoint_errors,
            "anchor": anchor_verification,
            "limitations": [
                "An anchor stored on the same host can be replaced by an attacker controlling that host.",
                "Signatures prove continuity and attribution, not the truth of recorded claims.",
                "Private keys without a passphrase rely on filesystem access controls.",
            ],
        }

    def import_anchor_identity(self, con: sqlite3.Connection, anchor: dict[str, Any]) -> None:
        verification = self.verify_anchor_document(anchor)
        if not verification["valid"]:
            raise ValueError("Cannot import identity from an invalid anchor")
        for key in anchor["keys"]:
            con.execute(
                """
                INSERT OR IGNORE INTO signing_keys (
                    key_id, algorithm, public_key_b64, predecessor_key_id,
                    trust_origin, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    key["key_id"],
                    key["algorithm"],
                    key["public_key_b64"],
                    key.get("predecessor_key_id"),
                    key.get("trust_origin") or "anchor_import",
                    key["created_at"],
                ),
            )
        for rotation in anchor["rotations"]:
            con.execute(
                """
                INSERT OR IGNORE INTO key_rotations (
                    rotation_id, old_key_id, new_key_id, payload_json,
                    payload_digest, old_signature_b64, new_signature_b64, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    rotation["rotation_id"],
                    rotation["old_key_id"],
                    rotation["new_key_id"],
                    rotation["payload_json"],
                    rotation["payload_digest"],
                    rotation["old_signature_b64"],
                    rotation["new_signature_b64"],
                    rotation["created_at"],
                ),
            )

    def adopt_restore_branch(
        self, con: sqlite3.Connection, previous_anchor: dict[str, Any]
    ) -> dict[str, Any]:
        anchor_verification = self.verify_anchor_document(previous_anchor)
        if not anchor_verification["valid"]:
            raise ValueError("Previous canonical anchor is invalid")
        self.import_anchor_identity(con, previous_anchor)
        previous_branch = self.active_branch(con)
        fork = self.latest_checkpoint(con, previous_branch["branch_id"])
        if fork is None:
            raise RuntimeError("A restore branch requires a local fork checkpoint")
        previous_canonical = previous_anchor["checkpoint"]
        branch_id = f"branch_{uuid.uuid4().hex}"
        created_at = utc_now()
        con.execute(
            """
            INSERT INTO audit_branches (
                branch_id, parent_branch_id, fork_checkpoint_id,
                fork_checkpoint_hash, previous_canonical_checkpoint_id,
                previous_canonical_checkpoint_hash, reason, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                branch_id,
                previous_branch["branch_id"],
                fork["checkpoint_id"],
                fork["checkpoint_hash"],
                previous_canonical["checkpoint_id"],
                previous_canonical["checkpoint_hash"],
                "verified_backup_restore",
                created_at,
            ),
        )
        con.execute(
            """
            INSERT INTO audit_branch_adoptions (
                adoption_id, branch_id, previous_branch_id, reason, adopted_at
            ) VALUES (?,?,?,?,?)
            """,
            (
                f"adopt_{uuid.uuid4().hex}",
                branch_id,
                previous_branch["branch_id"],
                "verified_backup_restore",
                created_at,
            ),
        )
        checkpoint = self.create_checkpoint(con, reason="restore_branch_adoption")
        return {
            "branch_id": branch_id,
            "parent_branch_id": previous_branch["branch_id"],
            "fork_checkpoint_id": fork["checkpoint_id"],
            "previous_canonical_checkpoint_id": previous_canonical["checkpoint_id"],
            "checkpoint": checkpoint,
        }

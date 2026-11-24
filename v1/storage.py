"""Fresh-workspace storage and signed transactional audit; no database migration."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .common import Conflict, Denied, Invalid, canonical, digest, identifier, now

SCHEMA = "hippocampus.v1.1"
EXCLUDED = {"meta", "audit_events", "object_heads", "event_fts", "event_fts_data", "event_fts_idx", "event_fts_content", "event_fts_docsize", "event_fts_config"}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def row_digest(row):
    return digest({k: {"blob_sha256": digest(v)} if isinstance(v, bytes) else v for k, v in dict(row).items()})


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".pending-write")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class Storage:
    @classmethod
    def create(cls, directory):
        root = Path(directory).absolute()
        # Refuse any pre-existing path BEFORE opening a file or SQLite handle.
        root.mkdir(parents=True, exist_ok=False)
        key = Ed25519PrivateKey.generate()
        (root / "signing.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        epoch = identifier("epoch")
        marker = {"schema": SCHEMA, "epoch": epoch, "data_class": "synthetic", "database": "canonical-v1.sqlite3"}
        atomic_json(root / "workspace.json", marker)
        path = root / marker["database"]
        con = sqlite3.connect(path)
        try:
            with con:
                con.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                con.executemany("INSERT INTO meta VALUES (?,?)", [("schema", SCHEMA), ("epoch", epoch)])
        finally:
            con.close()
        atomic_json(root / "anchor.json", {"sequence": 0, "hash": "0" * 64})
        return cls(root)

    def __init__(self, directory):
        self.root = Path(directory).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise Invalid("Expected an independently provisioned v1 workspace, not a database file")
        marker_path = self.root / "workspace.json"
        if not marker_path.is_file() or marker_path.is_symlink():
            raise Invalid("Unprovisioned workspace; no SQLite file was opened")
        self.marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if self.marker.get("schema") != SCHEMA or self.marker.get("data_class") != "synthetic" or self.marker.get("database") != "canonical-v1.sqlite3":
            raise Invalid("Unsupported workspace; no migration or fallback")
        self.path = self.root / "canonical-v1.sqlite3"
        if not self.path.is_file() or self.path.is_symlink():
            raise Invalid("Workspace database missing or redirected")
        self.key = serialization.load_pem_private_key((self.root / "signing.key").read_bytes(), password=None)
        self.epoch = self.marker["epoch"]
        with _LOCKS_GUARD:
            self.lock, self.local = _LOCKS.setdefault(str(self.root.resolve()).casefold(), (threading.RLock(), threading.local()))
        with self.connection() as con:
            if dict(con.execute("SELECT key,value FROM meta")) != {"schema": SCHEMA, "epoch": self.epoch}:
                raise Invalid("Workspace identity mismatch")
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            self.columns = {name: [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')] for name in names if name not in EXCLUDED}
            self.keys = {name: [r[1] for r in sorted(con.execute(f'PRAGMA table_info("{name}")'), key=lambda r: r[5]) if r[5]] for name in self.columns}
        with self.exclusive():
            self._recover_anchor()
            if not self.verify()["valid"]:
                raise Invalid("New-workspace integrity verification failed")

    @contextmanager
    def exclusive(self):
        """Serialize audit anchors with DB commits across threads and processes."""
        with self.lock:
            if getattr(self.local, "held", False):
                yield
                return
            with (self.root / "workspace.lock").open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self.local.held = True
                    yield
                finally:
                    self.local.held = False
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def connection(self, *, write=False):
        con = sqlite3.connect(self.path.as_uri() + "?mode=" + ("rw" if write else "ro"), uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        if not write:
            con.execute("PRAGMA query_only=ON")
        try:
            yield con
        finally:
            con.close()

    def _head(self, con):
        row = con.execute("SELECT sequence,event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        return {"sequence": row[0], "hash": row[1]} if row else {"sequence": 0, "hash": "0" * 64}

    def _recover_anchor(self):
        pending = self.root / "pending-anchor.json"
        if pending.exists():
            candidate = json.loads(pending.read_text(encoding="utf-8"))
            with self.connection() as con:
                current = self._head(con)
            old = json.loads((self.root / "anchor.json").read_text(encoding="utf-8"))
            if current == candidate:
                atomic_json(self.root / "anchor.json", current)
            elif current != old:
                raise Invalid("Unrecoverable audit/anchor disagreement")
            pending.unlink()

    def command(self, identity, name, key, payload, operation, *, guard=None):
        with self.exclusive():
            return self._command(identity, name, key, payload, operation, guard=guard)

    def _command(self, identity, name, key, payload, operation, *, guard=None):
        if not key:
            raise Invalid("Idempotency key required")
        receipt_id = identifier("receipt", identity.principal_id, name, key)
        with self.connection(write=True) as con:
            con.execute("BEGIN IMMEDIATE")
            self._recover_anchor()
            tx = Transaction(self, con)
            if guard:
                guard(tx)
            existing = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if existing:
                if existing["input_digest"] != digest(payload):
                    raise Conflict("Idempotency key reused with different input")
                return json.loads(existing["result_json"])
            try:
                result = operation(tx)
                tx.insert("receipts", {"id": receipt_id, "principal_id": identity.principal_id, "command": name,
                    "input_digest": digest(payload), "result_json": canonical(result)})
                head = self._head(con)
                event = {"epoch": self.epoch, "sequence": head["sequence"] + 1, "previous_hash": head["hash"],
                    "principal_id": identity.principal_id, "command": name, "changes": tx.changes, "created_at": now()}
                event_hash = digest(event)
                signature = base64.b64encode(self.key.sign(event_hash.encode())).decode()
                con.execute("INSERT INTO audit_events VALUES (?,?,?,?)", (event["sequence"], canonical(event), event_hash, signature))
                anchor = {"sequence": event["sequence"], "hash": event_hash}
                atomic_json(self.root / "pending-anchor.json", anchor)
                con.commit()
                atomic_json(self.root / "anchor.json", anchor)
                (self.root / "pending-anchor.json").unlink()
                return result
            except BaseException:
                con.rollback()
                raise

    def verify(self):
        with self.exclusive():
            return self._verify()

    def _verify(self):
        with self.connection() as con:
            previous, sequence, reconstructed = "0" * 64, 0, {}
            try:
                for row in con.execute("SELECT * FROM audit_events ORDER BY sequence"):
                    event = json.loads(row["event_json"])
                    if row["sequence"] != event["sequence"] or event["previous_hash"] != previous or event["sequence"] != sequence + 1 or event["epoch"] != self.epoch or digest(event) != row["event_hash"]:
                        return {"valid": False, "reason": "audit_chain"}
                    self.key.public_key().verify(base64.b64decode(row["signature"]), row["event_hash"].encode())
                    for change in event["changes"]:
                        index = (change["table"], change["key"])
                        if change["digest"] is None:
                            reconstructed.pop(index, None)
                        else:
                            reconstructed[index] = change["digest"]
                    previous, sequence = row["event_hash"], row["sequence"]
                actual = {(table, canonical({k: row[k] for k in self.keys[table]})): row_digest(row)
                    for table in self.columns for row in con.execute(f'SELECT * FROM "{table}"')}
                heads = {(r["table_name"], r["key_json"]): r["row_digest"] for r in con.execute("SELECT * FROM object_heads")}
                anchor = json.loads((self.root / "anchor.json").read_text(encoding="utf-8"))
                foreign_keys = list(con.execute("PRAGMA foreign_key_check"))
                canonical_events = sorted((r["id"], r["principal_id"], json.loads(r["data_json"])["content"])
                    for r in con.execute("SELECT * FROM records WHERE kind='event'"))
                indexed_events = sorted(tuple(r) for r in con.execute("SELECT id,principal_id,content FROM event_fts"))
                return {"valid": actual == reconstructed == heads and not foreign_keys and canonical_events == indexed_events
                        and anchor == {"sequence": sequence, "hash": previous},
                        "audited_rows": len(actual), "transactions": sequence, "foreign_key_errors": len(foreign_keys)}
            except Exception:
                return {"valid": False, "reason": "signature_or_anchor"}


class Transaction:
    def __init__(self, storage, con):
        self.storage, self.con, self.changes = storage, con, []

    def get(self, table, **keys):
        if table not in self.storage.columns or not set(keys) <= set(self.storage.columns[table]):
            raise Invalid("Unknown canonical table or column")
        condition = " AND ".join(f'"{key}"=?' for key in keys)
        return self.con.execute(f'SELECT * FROM "{table}" WHERE {condition}', tuple(keys.values())).fetchone()

    def _track(self, table, keys, row):
        key = canonical(keys)
        value = row_digest(row) if row is not None else None
        self.changes.append({"table": table, "key": key, "digest": value})
        if value is None:
            self.con.execute("DELETE FROM object_heads WHERE table_name=? AND key_json=?", (table, key))
        else:
            self.con.execute("INSERT OR REPLACE INTO object_heads VALUES (?,?,?)", (table, key, value))

    def insert(self, table, data):
        if table not in self.storage.columns or not set(data) <= set(self.storage.columns[table]):
            raise Invalid("Unknown canonical table or column")
        columns = ",".join(f'"{key}"' for key in data)
        self.con.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))
        keys = {k: data[k] for k in self.storage.keys[table]}
        row = self.get(table, **keys)
        self._track(table, keys, row)
        return row

    def update(self, table, keys, changes, *, expected=None):
        old = self.get(table, **keys)
        if old is None or (expected is not None and old["version"] != expected):
            raise Conflict("Missing record or expected-version conflict")
        if set(changes) & set(self.storage.keys[table]) or not set(changes) <= set(self.storage.columns[table]):
            raise Invalid("Invalid update columns")
        changes = {**changes, "version": old["version"] + 1}
        assignments = ",".join(f'"{key}"=?' for key in changes)
        condition = " AND ".join(f'"{key}"=?' for key in keys)
        self.con.execute(f'UPDATE "{table}" SET {assignments} WHERE {condition}', (*changes.values(), *keys.values()))
        row = self.get(table, **keys)
        self._track(table, keys, row)
        return row

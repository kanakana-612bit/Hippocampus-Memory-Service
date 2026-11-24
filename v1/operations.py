"""Operations for this new synthetic workspace only; never restore over a live DB."""
import base64
import json
import shutil
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .access import effective_identity, require
from .common import Denied, Invalid, canonical, digest, identifier, now
from .storage import SCHEMA, Storage, atomic_json

BACKUP_FILES = ("canonical-v1.sqlite3", "workspace.json", "signing.key", "anchor.json")


def public_key(storage):
    return base64.b64encode(storage.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def owner_operation(storage, identity, permission):
    require(identity, permission)
    with storage.connection() as con:
        identity, _ = effective_identity(con, identity)
    if identity.audience != "owner" or not identity.trusted_private:
        raise Denied("Workspace administration requires a private owner credential")
    # Until multi-owner erasure/export is implemented, do not provide partial operations
    # that might leave another owner's content in the returned whole-workspace backup.
    with storage.connection() as con:
        for table in storage.columns:
            if "principal_id" in storage.columns[table] and con.execute(f'SELECT 1 FROM "{table}" WHERE principal_id!=? LIMIT 1', (identity.principal_id,)).fetchone():
                raise Denied("Whole-workspace operation requires a single-owner workspace")


def backup(storage, identity, *, key):
    owner_operation(storage, identity, "operations.backup")
    backup_id = identifier("backup", storage.epoch, key)
    storage.command(identity, "operations.backup", key, {"backup_id": backup_id}, lambda tx: {"backup_id": backup_id})
    with storage.exclusive():
        if not storage.verify()["valid"]:
            raise Invalid("Backup requires a valid signed workspace")
        directory = storage.root / "backups" / backup_id
        if directory.exists():
            # Verify the prior result rather than overwriting a signed backup.
            verify_backup(directory, public_key(storage))
            return {"backup_id": backup_id, "path": str(directory), "trusted_public_key": public_key(storage)}
        directory.mkdir(parents=True, exist_ok=False)
        destination = sqlite3.connect(directory / "canonical-v1.sqlite3")
        try:
            with storage.connection() as source:
                source.backup(destination)
        finally:
            destination.close()
        for filename in BACKUP_FILES[1:]:
            shutil.copyfile(storage.root / filename, directory / filename)
        manifest = {"schema": "hippocampus.backup.v1", "workspace_schema": SCHEMA, "epoch": storage.epoch,
            "data_class": "synthetic", "created_at": now(), "files": {name: digest((directory / name).read_bytes()) for name in BACKUP_FILES}}
        signature = base64.b64encode(storage.key.sign(canonical(manifest).encode())).decode()
        atomic_json(directory / "backup.json", {"manifest": manifest, "signature": signature})
        return {"backup_id": backup_id, "path": str(directory), "trusted_public_key": public_key(storage)}


def verify_backup(directory, trusted_public_key):
    directory = Path(directory).absolute()
    marker = directory / "backup.json"
    if directory.is_symlink() or not marker.is_file() or marker.is_symlink():
        raise Invalid("Not a signed v1 backup; no SQLite file was opened")
    value = json.loads(marker.read_text(encoding="utf-8"))
    manifest = value["manifest"]
    if manifest.get("schema") != "hippocampus.backup.v1" or manifest.get("workspace_schema") != SCHEMA or manifest.get("data_class") != "synthetic" or set(manifest["files"]) != set(BACKUP_FILES):
        raise Invalid("Unsupported backup; no migration")
    if {p.name for p in directory.iterdir()} != set(BACKUP_FILES) | {"backup.json"}:
        raise Invalid("Unexpected backup entries")
    try:
        verifier = Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted_public_key))
        verifier.verify(base64.b64decode(value["signature"]), canonical(manifest).encode())
    except Exception as exc:
        raise Invalid("Backup signature did not match the independently trusted key") from exc
    for name, expected in manifest["files"].items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != expected:
            raise Invalid("Backup file digest mismatch")
    return manifest


def restore(directory, target, *, trusted_public_key):
    """Create a separate recovery workspace; active events are never overwritten."""
    target = Path(target).absolute()
    if target.exists():
        raise Invalid("Restore target must not exist; preserve newer live events")
    manifest = verify_backup(directory, trusted_public_key)
    target.mkdir(parents=True, exist_ok=False)
    for filename in BACKUP_FILES:
        shutil.copyfile(Path(directory) / filename, target / filename)
    storage = Storage(target)
    if storage.epoch != manifest["epoch"] or public_key(storage) != trusted_public_key:
        raise Invalid("Restored workspace identity does not match backup")
    return storage


def erase_synthetic_workspace(storage, identity, *, expected_epoch, confirm):
    """Explicit whole-owner purge, including local backups and staging, in dev mode."""
    owner_operation(storage, identity, "operations.erase")
    if confirm != "ERASE_SYNTHETIC_WORKSPACE" or expected_epoch != storage.epoch:
        raise Invalid("Explicit matching-epoch erasure confirmation required")
    root = storage.root.resolve(strict=True)
    if root != storage.root.absolute() or root.is_symlink() or root.parent == root:
        raise Invalid("Refusing redirected workspace erasure")
    with storage.exclusive():
        marker = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
        if marker != storage.marker or not storage.verify()["valid"]:
            raise Invalid("Workspace identity/integrity changed")
        # Only these known new-workspace paths may be removed. Unknown files require
        # an explicit operator decision and are not recursively guessed at.
        allowed = set(BACKUP_FILES) | {"workspace.lock", "backups"}
        children = list(root.iterdir())
        if any(p.name not in allowed or p.is_symlink() for p in children):
            raise Invalid("Unexpected workspace entries; explicit review required")
        backups = root / "backups"
        if backups.exists():
            if backups.resolve().parent != root:
                raise Invalid("Redirected backup root")
            for child in backups.iterdir():
                if child.resolve().parent != backups.resolve():
                    raise Invalid("Redirected backup directory")
                verify_backup(child, public_key(storage))
        for path in children:
            if path.name == "workspace.lock":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    (root / "workspace.lock").unlink()
    root.rmdir()
    return {"erased_epoch": expected_epoch, "local_workspace_and_backups_erased": True,
            "external_downloads": "Operator must separately erase any externally retained copies"}

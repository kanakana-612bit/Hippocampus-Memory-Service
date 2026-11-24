"""Versioned, read-only observation export with a transactional record outbox.

Optional extension to canonical schema 4. No display concepts belong here.
New databases enable the extension; existing databases require explicit enable.
"""
from __future__ import annotations

import argparse
import base64
import json
import uuid

from fastapi import Depends, HTTPException, Query

from .contracts import canonical, digest

CONTRACT = "hippocampus.record-feed.v1"
KINDS = (
    "memory", "event", "edge", "unresolved", "path", "run", "attempt", "observation",
    "state", "sandbox", "knowledge_claim", "knowledge_source", "knowledge_evidence",
    "knowledge_review", "knowledge_search", "knowledge_search_plan", "knowledge_search_attempt",
    "knowledge_search_observation",
)


def installed(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_meta'").fetchone() is not None


def install(con):
    """Caller owns the write transaction. Existing state is read through export."""
    if installed(con):
        if "recorded_at" in {r[1] for r in con.execute("PRAGMA table_info(observation_outbox)")}:
            return False
        con.execute("ALTER TABLE observation_outbox ADD COLUMN recorded_at REAL NOT NULL DEFAULT 0")
        con.execute("UPDATE observation_outbox SET recorded_at=unixepoch('now')")
        for suffix in ("insert", "update", "delete"):
            con.execute("DROP TRIGGER IF EXISTS observation_" + suffix)
    else:
        con.execute("CREATE TABLE observation_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.executemany("INSERT INTO observation_meta VALUES (?,?)", [("contract", CONTRACT), ("epoch", uuid.uuid4().hex)])
        con.execute("""CREATE TABLE observation_outbox(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, tx TEXT NOT NULL, principal TEXT NOT NULL,
            conversation TEXT, object_id TEXT NOT NULL, operation TEXT NOT NULL,
            record_json TEXT NOT NULL CHECK(json_valid(record_json)), recorded_at REAL NOT NULL)""")
        con.execute("CREATE INDEX observation_scope ON observation_outbox(principal,conversation,seq)")
    con.execute("INSERT OR IGNORE INTO observation_meta VALUES ('retention_floor','0')")
    kinds = ",".join("'" + k + "'" for k in KINDS)
    for action, alias, operation in (("INSERT", "NEW", "upsert"), ("UPDATE OF data_json,version", "NEW", "upsert"), ("DELETE", "OLD", "delete")):
        suffix = action.split()[0].lower()
        record = (f"json_object('id',{alias}.id,'kind',{alias}.kind,'principal_id',{alias}.principal_id,"
                  f"'conversation_id',{alias}.conversation_id,'version',{alias}.version,"
                  f"'created_at',{alias}.created_at,'updated_at',{alias}.updated_at,"
                  f"'data',json({alias}.data_json))")
        if operation == "delete":
            record = f"json_object('id',OLD.id,'kind',OLD.kind,'version',OLD.version)"
        # Remove old content from the observation copy on explicit record deletion.
        purge = "DELETE FROM observation_outbox WHERE object_id=OLD.id;" if operation == "delete" else ""
        con.execute(f"""CREATE TRIGGER observation_{suffix} AFTER {action} ON records
            WHEN {alias}.kind IN ({kinds}) BEGIN
            {purge}
            INSERT INTO observation_outbox(tx,principal,conversation,object_id,operation,record_json,recorded_at)
            VALUES(observation_tx(),{alias}.principal_id,{alias}.conversation_id,{alias}.id,'{operation}',{record},unixepoch('now')); END""")
    return True


def _metadata(con, store):
    if not installed(con):
        raise HTTPException(503, "Observation extension is not enabled")
    meta = dict(con.execute("SELECT key,value FROM observation_meta"))
    # Signed restore branches invalidate all previously issued feed cursors,
    # even if the restored outbox happens to have the same numeric high-water.
    meta["epoch"] = digest([meta["epoch"], store.security.active_branch(con)["branch_id"]])
    return meta


def _scope(principal, conversation):
    return digest([principal, conversation])


def _high(con, meta):
    return max(int(meta.get("retention_floor", 0)), con.execute("SELECT coalesce(max(seq),0) FROM observation_outbox").fetchone()[0])


def prune(store, keep_seconds=3600):
    """Bound the transport copy; never remove canonical records or split a tx."""
    with store.connection(write=True) as con:
        if not installed(con) or "recorded_at" not in {r[1] for r in con.execute("PRAGMA table_info(observation_outbox)")}:
            return 0
        old = list(con.execute("SELECT tx,max(seq) AS hi FROM observation_outbox GROUP BY tx HAVING max(recorded_at)<unixepoch('now')-?", (keep_seconds,)))
        if not old:
            return 0
        floor = max(int(dict(con.execute("SELECT key,value FROM observation_meta")).get("retention_floor", 0)), max(r["hi"] for r in old))
        con.executemany("DELETE FROM observation_outbox WHERE tx=?", [(r["tx"],) for r in old])
        con.execute("INSERT OR REPLACE INTO observation_meta VALUES ('retention_floor',?)", (str(floor),))
        return len(old)


async def maintain(store):
    import asyncio
    import sqlite3
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(prune, store)
        except (ValueError, sqlite3.Error):
            pass  # Offline maintenance or a busy writer: retry at the next interval.


def cursor(epoch, sequence, scope):
    return base64.urlsafe_b64encode(canonical([epoch, sequence, scope]).encode()).decode().rstrip("=")


def _decode_cursor(value, epoch, scope):
    try:
        e, seq, s = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if e != epoch or s != scope or type(seq) is not int or seq < 0:
            raise ValueError()
        return seq
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(409, "Observation cursor requires resynchronization") from None


def export(store, principal, conversation=None):
    with store.connection() as con:
        con.execute("BEGIN")  # SQLite otherwise starts snapshots separately per SELECT.
        meta = _metadata(con, store)
        high = _high(con, meta)
        where, args = "principal_id=?", [principal]
        if conversation is not None:
            where += " AND conversation_id=?"
            args.append(conversation)
        rows = con.execute("SELECT * FROM records WHERE " + where + " ORDER BY id", args)
        records = [store.decode(r) for r in rows if r["kind"] in KINDS]
        return {"contract": CONTRACT, "epoch": meta["epoch"], "cursor": cursor(meta["epoch"], high, _scope(principal, conversation)),
                "records": records, "scope": {"conversation_id": conversation}, "precision": "committed_records"}


def changes(store, principal, after, conversation=None, limit=128):
    with store.connection() as con:
        con.execute("BEGIN")
        meta = _metadata(con, store)
        scope = _scope(principal, conversation)
        start = _decode_cursor(after, meta["epoch"], scope)
        high = _high(con, meta)
        if start < int(meta.get("retention_floor", 0)):
            raise HTTPException(409, "Observation retention expired; resynchronize")
        if start > high:
            raise HTTPException(409, "Observation feed was restored; resynchronize")
        where, args = "seq>? AND principal=?", [start, principal]
        if conversation is not None:
            where += " AND conversation=?"
            args.append(conversation)
        rows = list(con.execute("SELECT * FROM observation_outbox WHERE " + where + " ORDER BY seq LIMIT ?", args + [limit]))
        if rows:
            # Never split a canonical transaction at a pagination boundary.
            rows += list(con.execute("SELECT * FROM observation_outbox WHERE " + where + " AND seq>? AND tx=? ORDER BY seq",
                                     args + [rows[-1]["seq"], rows[-1]["tx"]]))
        end = rows[-1]["seq"] if rows else high
        return {"contract": CONTRACT, "epoch": meta["epoch"], "cursor": cursor(meta["epoch"], end, scope),
                "has_more": end < high, "changes": [{"transaction_id": r["tx"], "operation": r["operation"],
                    "record": json.loads(r["record_json"])} for r in rows]}


def register(application, prefix, require, svc):
    root = prefix + "/observation"

    @application.get(root + "/export")
    def read_export(conversation_id: str | None = None, identity=Depends(require("read")), service=Depends(svc)):
        return export(service.db, identity.principal, conversation_id)

    @application.get(root + "/changes")
    def read_changes(after: str, conversation_id: str | None = None, limit: int = Query(128, ge=1, le=1000),
                     identity=Depends(require("read")), service=Depends(svc)):
        return changes(service.db, identity.principal, after, conversation_id, limit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    from .storage import Storage
    from pathlib import Path
    if not Path(args.db).is_file():
        parser.error("An existing canonical database is required")
    store = Storage(args.db)
    with store.connection() as con:
        enabled = installed(con)
    if args.enable:
        if not args.service_stopped:
            parser.error("Stop existing writers, then explicitly pass --service-stopped before enabling triggers")
        store.backup("pre-observation-v1")
        with store.connection(write=True) as con:
            install(con)
        enabled = True
    print(canonical({"contract": CONTRACT, "enabled": enabled, "canonical_schema_unchanged": True}))


if __name__ == "__main__":
    main()

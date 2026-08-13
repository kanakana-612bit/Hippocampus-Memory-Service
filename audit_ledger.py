from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


GENESIS_HASH = "0" * 64
LEDGER_FORMAT = "hippocampus-audit-v1"
OBJECT_TABLES = {
    "raw_message": "raw_messages",
    "memory_trace": "memory_traces",
    "memory": "memories",
}
POINTER_FIELDS = {"latest_audit_event_id", "latest_object_digest"}
TERMINAL_EVENT_SUFFIXES = (".deleted", ".forgotten")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_derivations(value: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value or []:
        if isinstance(item, str):
            item = {
                "object_type": "raw_message",
                "object_id": item,
                "relation": "derived_from",
            }
        if not isinstance(item, dict):
            continue
        object_type = str(item.get("object_type") or "raw_message").strip()
        object_id = str(item.get("object_id") or "").strip()
        relation = str(item.get("relation") or "derived_from").strip()
        if not object_id:
            continue
        key = (object_type, object_id, relation)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "object_type": object_type,
                "object_id": object_id,
                "relation": relation,
            }
        )
    return sorted(
        normalized,
        key=lambda item: (item["object_type"], item["object_id"], item["relation"]),
    )


def snapshot_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {key: value for key, value in data.items() if key not in POINTER_FIELDS}


def object_digest(row: sqlite3.Row | dict[str, Any]) -> str:
    return sha256_text(canonical_json(snapshot_from_row(row)))


class AuditLedger:
    def available(self, con: sqlite3.Connection) -> bool:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone() is not None

    def append_object_event(
        self,
        con: sqlite3.Connection,
        *,
        event_type: str,
        object_type: str,
        object_id: str,
        actor_id: str | None = None,
        actor_role: str = "unknown",
        source_channel: str = "internal",
        content_origin: str = "derived",
        extractor: str | None = None,
        derivations: Any = None,
        event_time: str | None = None,
        received_at: str | None = None,
        integrity_tier: str = "routine",
        payload: dict[str, Any] | None = None,
        update_pointer: bool = True,
    ) -> dict[str, Any] | None:
        if not self.available(con):
            return None
        if object_type not in OBJECT_TABLES:
            raise ValueError(f"Unsupported audited object_type: {object_type}")
        table = OBJECT_TABLES[object_type]
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()
        if row is None and not event_type.endswith(TERMINAL_EVENT_SUFFIXES):
            raise KeyError(f"{object_type} not found: {object_id}")

        current_digest = object_digest(row) if row is not None else None
        persisted_at = utc_now()
        normalized_derivations = normalize_derivations(derivations)
        derivation_json = canonical_json(normalized_derivations)
        payload_json = canonical_json(
            {
                "format": LEDGER_FORMAT,
                "operation": payload or {},
            }
        )
        payload_digest = sha256_text(payload_json)
        previous = con.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous["event_hash"]) if previous else GENESIS_HASH
        event_id = f"audit_{uuid.uuid4().hex}"
        material = {
            "format": LEDGER_FORMAT,
            "event_id": event_id,
            "event_type": event_type,
            "object_type": object_type,
            "object_id": object_id,
            "actor_id": actor_id,
            "actor_role": actor_role or "unknown",
            "source_channel": source_channel or "internal",
            "content_origin": content_origin or "derived",
            "extractor": extractor,
            "derivation_json": derivation_json,
            "payload_digest": payload_digest,
            "object_digest": current_digest,
            "previous_event_hash": previous_hash,
            "event_time": event_time or persisted_at,
            "received_at": received_at or persisted_at,
            "persisted_at": persisted_at,
            "integrity_tier": integrity_tier,
        }
        event_hash = sha256_text(canonical_json(material))
        cursor = con.execute(
            """
            INSERT INTO audit_events (
                event_id, event_type, object_type, object_id,
                actor_id, actor_role, source_channel, content_origin, extractor,
                derivation_json, payload_json, payload_digest, object_digest,
                previous_event_hash, event_hash, event_time, received_at,
                persisted_at, integrity_tier, format_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                event_type,
                object_type,
                object_id,
                actor_id,
                material["actor_role"],
                material["source_channel"],
                material["content_origin"],
                extractor,
                derivation_json,
                payload_json,
                payload_digest,
                current_digest,
                previous_hash,
                event_hash,
                material["event_time"],
                material["received_at"],
                persisted_at,
                integrity_tier,
                LEDGER_FORMAT,
            ),
        )
        sequence = int(cursor.lastrowid)
        for derivation in normalized_derivations:
            con.execute(
                """
                INSERT OR IGNORE INTO provenance_edges (
                    edge_id, source_object_type, source_object_id,
                    target_object_type, target_object_id, relation,
                    audit_event_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    f"edge_{uuid.uuid4().hex}",
                    derivation["object_type"],
                    derivation["object_id"],
                    object_type,
                    object_id,
                    derivation["relation"],
                    event_id,
                    persisted_at,
                ),
            )
        if row is not None and update_pointer:
            con.execute(
                f"UPDATE {table} SET latest_audit_event_id=?, latest_object_digest=? WHERE id=?",
                (event_id, current_digest, object_id),
            )
        return {
            "sequence": sequence,
            **material,
            "event_hash": event_hash,
            "derivations": normalized_derivations,
        }

    @staticmethod
    def _event_material(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "format": row["format_version"],
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "source_channel": row["source_channel"],
            "content_origin": row["content_origin"],
            "extractor": row["extractor"],
            "derivation_json": row["derivation_json"],
            "payload_digest": row["payload_digest"],
            "object_digest": row["object_digest"],
            "previous_event_hash": row["previous_event_hash"],
            "event_time": row["event_time"],
            "received_at": row["received_at"],
            "persisted_at": row["persisted_at"],
            "integrity_tier": row["integrity_tier"],
        }

    def verify(self, con: sqlite3.Connection, verify_objects: bool = True) -> dict[str, Any]:
        rows = con.execute("SELECT * FROM audit_events ORDER BY sequence").fetchall()
        chain_errors: list[dict[str, Any]] = []
        previous_hash = GENESIS_HASH
        for row in rows:
            reasons: list[str] = []
            if row["previous_event_hash"] != previous_hash:
                reasons.append("previous_event_hash_mismatch")
            expected_payload_digest = sha256_text(str(row["payload_json"]))
            if row["payload_digest"] != expected_payload_digest:
                reasons.append("payload_digest_mismatch")
            expected_event_hash = sha256_text(canonical_json(self._event_material(row)))
            if row["event_hash"] != expected_event_hash:
                reasons.append("event_hash_mismatch")
            if reasons:
                chain_errors.append(
                    {
                        "sequence": row["sequence"],
                        "event_id": row["event_id"],
                        "reasons": reasons,
                    }
                )
            previous_hash = str(row["event_hash"])

        object_errors: list[dict[str, Any]] = []
        checked_objects = 0
        if verify_objects:
            latest_rows = con.execute(
                """
                SELECT event_id, event_type, object_type, object_id, object_digest
                FROM audit_events AS event
                WHERE sequence=(
                    SELECT MAX(candidate.sequence)
                    FROM audit_events AS candidate
                    WHERE candidate.object_type=event.object_type
                      AND candidate.object_id=event.object_id
                )
                """
            ).fetchall()
            for latest in latest_rows:
                if str(latest["event_type"]).endswith(TERMINAL_EVENT_SUFFIXES):
                    continue
                table = OBJECT_TABLES.get(str(latest["object_type"]))
                if not table:
                    continue
                checked_objects += 1
                current = con.execute(
                    f"SELECT * FROM {table} WHERE id=?", (latest["object_id"],)
                ).fetchone()
                reasons: list[str] = []
                if current is None:
                    reasons.append("current_object_missing")
                else:
                    current_digest = object_digest(current)
                    if current_digest != latest["object_digest"]:
                        reasons.append("current_object_digest_mismatch")
                    if current["latest_audit_event_id"] != latest["event_id"]:
                        reasons.append("latest_event_pointer_mismatch")
                    if current["latest_object_digest"] != latest["object_digest"]:
                        reasons.append("object_digest_pointer_mismatch")
                if reasons:
                    object_errors.append(
                        {
                            "object_type": latest["object_type"],
                            "object_id": latest["object_id"],
                            "event_id": latest["event_id"],
                            "reasons": reasons,
                        }
                    )

        expected_edges: set[tuple[str, str, str, str, str, str]] = set()
        for row in rows:
            for derivation in json.loads(row["derivation_json"] or "[]"):
                expected_edges.add(
                    (
                        str(derivation["object_type"]),
                        str(derivation["object_id"]),
                        str(row["object_type"]),
                        str(row["object_id"]),
                        str(derivation["relation"]),
                        str(row["event_id"]),
                    )
                )
        edge_rows = con.execute(
            """
            SELECT source_object_type, source_object_id, target_object_type,
                   target_object_id, relation, audit_event_id
            FROM provenance_edges
            """
        ).fetchall()
        actual_edges = {
            (
                str(row["source_object_type"]),
                str(row["source_object_id"]),
                str(row["target_object_type"]),
                str(row["target_object_id"]),
                str(row["relation"]),
                str(row["audit_event_id"]),
            )
            for row in edge_rows
        }
        missing_edges = sorted(expected_edges - actual_edges)
        unexpected_edges = sorted(actual_edges - expected_edges)
        provenance_errors = {
            "missing_edges": [list(edge) for edge in missing_edges],
            "unexpected_edges": [list(edge) for edge in unexpected_edges],
        }

        head = rows[-1] if rows else None
        return {
            "valid": not chain_errors and not object_errors and not missing_edges and not unexpected_edges,
            "chain_valid": not chain_errors,
            "current_state_valid": not object_errors,
            "provenance_valid": not missing_edges and not unexpected_edges,
            "event_count": len(rows),
            "checked_objects": checked_objects,
            "head_event_id": head["event_id"] if head else None,
            "head_event_hash": head["event_hash"] if head else GENESIS_HASH,
            "chain_errors": chain_errors,
            "object_errors": object_errors,
            "provenance_errors": provenance_errors,
            "limitations": [
                "Unsigned local hash chains cannot prove an unchanged tail without an external checkpoint.",
                "Integrity verification does not establish that recorded claims are true.",
            ],
        }

    def list_events(
        self,
        con: sqlite3.Connection,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        limit: int = 100,
        include_payload: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if object_type:
            clauses.append("object_type=?")
            params.append(object_type)
        if object_id:
            clauses.append("object_id=?")
            params.append(object_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = con.execute(
            f"SELECT * FROM audit_events {where} ORDER BY sequence DESC LIMIT ?",
            (*params, max(1, min(1000, int(limit)))),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["derivations"] = json.loads(item.pop("derivation_json") or "[]")
            payload_json = item.pop("payload_json")
            if include_payload:
                item["payload"] = json.loads(payload_json)
            result.append(item)
        return result

    def provenance(self, con: sqlite3.Connection, object_type: str, object_id: str) -> dict[str, Any]:
        events = self.list_events(
            con,
            object_type=object_type,
            object_id=object_id,
            limit=500,
            include_payload=False,
        )
        incoming = con.execute(
            """
            SELECT * FROM provenance_edges
            WHERE target_object_type=? AND target_object_id=?
            ORDER BY created_at, edge_id
            """,
            (object_type, object_id),
        ).fetchall()
        outgoing = con.execute(
            """
            SELECT * FROM provenance_edges
            WHERE source_object_type=? AND source_object_id=?
            ORDER BY created_at, edge_id
            """,
            (object_type, object_id),
        ).fetchall()
        return {
            "object_type": object_type,
            "object_id": object_id,
            "events": events,
            "incoming": [dict(row) for row in incoming],
            "outgoing": [dict(row) for row in outgoing],
        }

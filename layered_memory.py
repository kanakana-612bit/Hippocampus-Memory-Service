from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_TYPES = ("episodic", "semantic", "prospective", "procedural", "embodied")
LEGACY_MEMORY_TYPES = ("episodic", "project", "persistent")
LEGACY_TYPE_MAP = {
    "episodic": "episodic",
    "project": "prospective",
    "persistent": "semantic",
}
ACQUISITION_MODES = ("automatic", "user_explicit", "reviewed", "system_derived")
EPISTEMIC_STATUSES = ("observed", "inferred", "confirmed", "disputed")
TRACE_STAGES = ("proto", "candidate")
TRACE_STATUSES = ("active", "review", "consolidated", "archived")

DEFAULT_RECORD_THRESHOLD = 0.65
DEFAULT_REVIEW_THRESHOLD = 0.82
DEFAULT_DELETE_THRESHOLD = 0.15
DEFAULT_DAILY_DECAY_RATE = 0.90
DEFAULT_RECALL_BOOST = 0.10
DEFAULT_SPACED_STABILITY_BOOST = 0.025


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = lo
    return max(lo, min(hi, numeric))


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def parse_time(value: str | None, fallback: datetime | None = None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback or datetime.now(timezone.utc)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def query_terms(text: str) -> list[str]:
    normalized = normalize_text(text)
    ascii_terms = re.findall(r"[a-z0-9_./:-]{2,}", normalized)
    quoted_terms = re.findall(r"[「『\"]([^」』\"]{2,})[」』\"]", text or "")
    japanese_chunks = re.findall(r"[\u3040-\u30ff\u3400-\u9fffー・]{2,}", text or "")
    japanese_terms: list[str] = []
    for chunk in japanese_chunks:
        japanese_terms.append(chunk)
        parts = re.split(r"(?:について|として|から|まで|より|など|の|と|に|を|は|が|で|へ|や|も|、|。)", chunk)
        japanese_terms.extend(part for part in parts if len(part) >= 2)
    seen: set[str] = set()
    result: list[str] = []
    for term in ascii_terms + quoted_terms + japanese_terms:
        normalized_term = normalize_text(term)
        if normalized_term and normalized_term not in seen:
            seen.add(normalized_term)
            result.append(normalized_term)
    return result[:20]


def retention_score(
    activation: Any,
    salience: Any,
    stability: Any,
    continuity_score: Any = 0.0,
) -> float:
    return clamp(
        0.35 * clamp(activation)
        + 0.30 * clamp(salience)
        + 0.25 * clamp(stability)
        + 0.10 * clamp(continuity_score)
    )


def decay_activation(
    activation: Any,
    elapsed_days: float,
    salience: Any,
    stability: Any,
    daily_decay_rate: float = DEFAULT_DAILY_DECAY_RATE,
) -> float:
    if elapsed_days <= 0:
        return clamp(activation)
    effective_rate = clamp(
        daily_decay_rate + 0.035 * clamp(salience) + 0.045 * clamp(stability),
        0.0,
        0.995,
    )
    return clamp(clamp(activation) * math.pow(effective_rate, elapsed_days))


def _as_optional_score(value: Any) -> float | None:
    return None if value is None else clamp(value)


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back a context transaction, then release the DB handle."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class LayeredMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def initialize(self, con: sqlite3.Connection | None = None) -> dict[str, int]:
        own = con is None
        con = con or self.connect()
        try:
            migrated = self.migrate_legacy_memories(con)
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
                (2, "separate short-term traces and canonical long-term memories", utc_now()),
            )
            if own:
                con.commit()
            return migrated
        finally:
            if own:
                con.close()

    def migrate_legacy_memories(self, con: sqlite3.Connection) -> dict[str, int]:
        counts = {memory_type: 0 for memory_type in LEGACY_MEMORY_TYPES}
        migration_done = con.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
        if migration_done:
            return counts

        table_map = {
            "episodic": "episodic_memories",
            "project": "project_memories",
            "persistent": "persistent_memories",
        }
        for legacy_type, table in table_map.items():
            for row in con.execute(f"SELECT * FROM {table}").fetchall():
                item = self.legacy_row_to_item(legacy_type, row)
                if self.upsert_memory(item, con=con, only_if_missing=True):
                    counts[legacy_type] += 1
        return counts

    def legacy_row_to_item(self, legacy_type: str, row: sqlite3.Row) -> dict[str, Any]:
        if legacy_type == "episodic":
            legacy = {
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "date": row["date"],
                "keywords": loads(row["keywords_json"], []),
                "entities": loads(row["entities_json"], []),
                "emotion": {
                    "valence": row["emotion_valence"],
                    "intensity": row["emotion_intensity"],
                    "tags": loads(row["emotion_tags_json"], []),
                },
                "importance_score": row["importance_score"],
                "recency_score": row["recency_score"],
                "repetition_score": row["repetition_score"],
                "continuity_score": row["continuity_score"],
                "last_recalled_at": row["last_recalled_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "source": loads(row["source_json"], {}),
                "retention": loads(row["retention_json"], {}),
                "confidence": row["confidence"],
                "evidence_type": row["evidence_type"],
                "wording_policy": row["wording_policy"],
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        elif legacy_type == "project":
            legacy = {
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "status": row["status"],
                "current_state": loads(row["current_state_json"], []),
                "open_questions": loads(row["open_questions_json"], []),
                "related_episodes": loads(row["related_episodes_json"], []),
                "keywords": loads(row["keywords_json"], []),
                "importance_score": row["importance_score"],
                "last_recalled_at": row["last_recalled_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "source": loads(row["source_json"], {}),
                "confidence": row["confidence"],
                "evidence_type": row["evidence_type"],
                "wording_policy": row["wording_policy"],
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        else:
            legacy = {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "category": row["category"],
                "keywords": loads(row["keywords_json"], []),
                "importance_score": row["importance_score"],
                "last_recalled_at": row["last_recalled_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "source": loads(row["source_json"], {}),
                "confidence": row["confidence"],
                "evidence_type": row["evidence_type"],
                "wording_policy": row["wording_policy"],
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        return self.legacy_to_memory(legacy_type, legacy)

    def legacy_to_memory(self, legacy_type: str, legacy: dict[str, Any]) -> dict[str, Any]:
        if legacy_type not in LEGACY_TYPE_MAP:
            raise ValueError(f"Unknown legacy memory type: {legacy_type}")

        memory_type = LEGACY_TYPE_MAP[legacy_type]
        category = normalize_text(str(legacy.get("category", "")))
        if legacy_type == "persistent":
            if any(term in category for term in ("procedure", "workflow", "tool", "手順")):
                memory_type = "procedural"
            elif any(term in category for term in ("embodied", "physical", "身体")):
                memory_type = "embodied"

        confirmed = bool(legacy.get("user_confirmed")) or legacy.get("evidence_type") == "explicit"
        importance = clamp(legacy.get("importance_score", 0.5))
        activation = clamp(legacy.get("recency_score", importance))
        if confirmed:
            stability = 1.0
        elif legacy_type == "persistent":
            stability = 0.55
        elif legacy_type == "project":
            stability = 0.40
        else:
            stability = 0.28
        continuity = clamp(legacy.get("continuity_score", 0.6 if legacy_type == "project" else 0.0))
        source = dict(legacy.get("source") or {})
        source_event_ids = self.source_event_ids(source)
        created_at = legacy.get("created_at") or utc_now()
        updated_at = legacy.get("updated_at") or created_at

        metadata = {"legacy_memory_type": legacy_type}
        if legacy_type == "episodic":
            metadata.update(
                {
                    "date": legacy.get("date"),
                    "emotion": legacy.get("emotion") or {},
                    "repetition_score": clamp(legacy.get("repetition_score", 0.0)),
                    "retention": legacy.get("retention") or {},
                    "wording_policy": legacy.get("wording_policy", "tentative"),
                }
            )
        elif legacy_type == "project":
            metadata.update(
                {
                    "status": legacy.get("status", "active"),
                    "current_state": legacy.get("current_state") or [],
                    "open_questions": legacy.get("open_questions") or [],
                    "related_episodes": legacy.get("related_episodes") or [],
                    "wording_policy": legacy.get("wording_policy", "tentative"),
                }
            )
        else:
            metadata.update(
                {
                    "category": legacy.get("category", "preference"),
                    "wording_policy": legacy.get("wording_policy", "confirmed" if confirmed else "tentative"),
                }
            )

        return {
            "id": legacy["id"],
            "memory_type": memory_type,
            "title": legacy.get("title") or "Untitled memory",
            "content": legacy.get("summary") if legacy_type != "persistent" else legacy.get("content", ""),
            "keywords": legacy.get("keywords") or [],
            "entities": legacy.get("entities") or [],
            "acquisition_mode": "user_explicit" if confirmed else "system_derived",
            "epistemic_status": "confirmed" if confirmed else "inferred",
            "epistemic_confidence": clamp(legacy.get("confidence", 1.0 if confirmed else 0.5)),
            "activation": activation,
            "salience": importance,
            "stability": stability,
            "continuity_score": continuity,
            "retention_score": retention_score(activation, importance, stability, continuity),
            "last_recalled_at": legacy.get("last_recalled_at"),
            "pinned": bool(legacy.get("pinned")),
            "archived": bool(legacy.get("archived")),
            "evidence_summary": str(source.get("evidence_summary", "")),
            "source_event_ids": source_event_ids,
            "source": source,
            "metadata": metadata,
            "consolidated_at": created_at,
            "legacy_memory_type": legacy_type,
            "legacy_memory_id": legacy["id"],
            "created_at": created_at,
            "updated_at": updated_at,
        }

    @staticmethod
    def source_event_ids(source: dict[str, Any]) -> list[str]:
        values: list[Any] = []
        for key in ("source_event_ids", "message_ids", "source_message_ids"):
            item = source.get(key)
            values.extend(item if isinstance(item, list) else ([item] if item else []))
        for key in ("message_id", "turn_id", "event_id"):
            if source.get(key):
                values.append(source[key])
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value)
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    def sync_legacy_memory(
        self,
        legacy_type: str,
        legacy: dict[str, Any],
        con: sqlite3.Connection | None = None,
    ) -> str:
        item = self.legacy_to_memory(legacy_type, legacy)
        self.upsert_memory(item, con=con)
        return str(item["id"])

    def upsert_memory(
        self,
        item: dict[str, Any],
        con: sqlite3.Connection | None = None,
        only_if_missing: bool = False,
    ) -> bool:
        own = con is None
        con = con or self.connect()
        try:
            memory_type = str(item.get("memory_type", ""))
            if memory_type not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory_type: {memory_type}")
            acquisition_mode = str(item.get("acquisition_mode", "system_derived"))
            if acquisition_mode not in ACQUISITION_MODES:
                raise ValueError(f"Unknown acquisition_mode: {acquisition_mode}")
            epistemic_status = str(item.get("epistemic_status", "inferred"))
            if epistemic_status not in EPISTEMIC_STATUSES:
                raise ValueError(f"Unknown epistemic_status: {epistemic_status}")

            memory_id = str(item.get("id") or new_id("memory"))
            if only_if_missing and con.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone():
                return False
            now = utc_now()
            activation = clamp(item.get("activation", 0.5))
            salience = clamp(item.get("salience", 0.5))
            stability = clamp(item.get("stability", 0.3))
            continuity = clamp(item.get("continuity_score", 0.0))
            computed_retention = retention_score(activation, salience, stability, continuity)
            created_at = item.get("created_at") or now
            last_decayed_at = item.get("last_decayed_at") or item.get("updated_at") or created_at

            con.execute(
                """
                INSERT INTO memories (
                    id, memory_type, title, content, keywords_json, entities_json,
                    acquisition_mode, epistemic_status, epistemic_confidence,
                    activation, salience, stability, continuity_score, retention_score,
                    last_recalled_at, recall_count, last_decayed_at, expires_at,
                    pinned, archived, evidence_summary, source_event_ids_json,
                    source_json, metadata_json, observation_statement, perspective,
                    evidence_kind, observation_fidelity, source_reliability,
                    world_hypothesis, consolidated_at, legacy_memory_type,
                    legacy_memory_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    memory_type=excluded.memory_type,
                    title=excluded.title,
                    content=excluded.content,
                    keywords_json=excluded.keywords_json,
                    entities_json=excluded.entities_json,
                    acquisition_mode=excluded.acquisition_mode,
                    epistemic_status=excluded.epistemic_status,
                    epistemic_confidence=excluded.epistemic_confidence,
                    activation=MAX(memories.activation, excluded.activation),
                    salience=excluded.salience,
                    stability=MAX(memories.stability, excluded.stability),
                    continuity_score=excluded.continuity_score,
                    retention_score=MAX(memories.retention_score, excluded.retention_score),
                    pinned=excluded.pinned,
                    archived=excluded.archived,
                    evidence_summary=excluded.evidence_summary,
                    source_event_ids_json=excluded.source_event_ids_json,
                    source_json=excluded.source_json,
                    metadata_json=excluded.metadata_json,
                    observation_statement=excluded.observation_statement,
                    perspective=excluded.perspective,
                    evidence_kind=excluded.evidence_kind,
                    observation_fidelity=excluded.observation_fidelity,
                    source_reliability=excluded.source_reliability,
                    world_hypothesis=excluded.world_hypothesis,
                    legacy_memory_type=excluded.legacy_memory_type,
                    legacy_memory_id=excluded.legacy_memory_id,
                    updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    memory_type,
                    str(item.get("title") or "Untitled memory"),
                    str(item.get("content") or ""),
                    dumps(item.get("keywords") or []),
                    dumps(item.get("entities") or []),
                    acquisition_mode,
                    epistemic_status,
                    clamp(item.get("epistemic_confidence", 0.5)),
                    activation,
                    salience,
                    stability,
                    continuity,
                    computed_retention,
                    item.get("last_recalled_at"),
                    int(item.get("recall_count", 0)),
                    last_decayed_at,
                    item.get("expires_at"),
                    1 if item.get("pinned") else 0,
                    1 if item.get("archived") else 0,
                    str(item.get("evidence_summary") or ""),
                    dumps(item.get("source_event_ids") or []),
                    dumps(item.get("source") or {}),
                    dumps(item.get("metadata") or {}),
                    item.get("observation_statement"),
                    item.get("perspective"),
                    item.get("evidence_kind"),
                    _as_optional_score(item.get("observation_fidelity")),
                    _as_optional_score(item.get("source_reliability")),
                    item.get("world_hypothesis"),
                    item.get("consolidated_at") or now,
                    item.get("legacy_memory_type"),
                    item.get("legacy_memory_id"),
                    created_at,
                    item.get("updated_at") or now,
                ),
            )
            self.index_memory(con, memory_id)
            if own:
                con.commit()
            return True
        finally:
            if own:
                con.close()

    def create_trace(self, item: dict[str, Any]) -> dict[str, Any]:
        trace_stage = str(item.get("trace_stage", "proto"))
        if trace_stage not in TRACE_STAGES:
            raise ValueError(f"Unknown trace_stage: {trace_stage}")
        candidate_memory_type = item.get("candidate_memory_type")
        if candidate_memory_type is not None and candidate_memory_type not in MEMORY_TYPES:
            raise ValueError(f"Unknown candidate_memory_type: {candidate_memory_type}")
        acquisition_mode = str(item.get("acquisition_mode", "automatic"))
        epistemic_status = str(item.get("epistemic_status", "inferred"))
        if acquisition_mode not in ACQUISITION_MODES:
            raise ValueError(f"Unknown acquisition_mode: {acquisition_mode}")
        if epistemic_status not in EPISTEMIC_STATUSES:
            raise ValueError(f"Unknown epistemic_status: {epistemic_status}")
        content = str(item.get("content") or "").strip()
        if not content:
            raise ValueError("content is required.")

        trace_id = str(item.get("id") or new_id("trace"))
        now = utc_now()
        activation = clamp(item.get("activation", 0.5))
        salience = clamp(item.get("salience", 0.5))
        stability = clamp(item.get("stability", 0.1))
        continuity = clamp(item.get("continuity_score", 0.0))
        score = retention_score(activation, salience, stability, continuity)
        record_threshold = clamp(item.get("record_threshold", DEFAULT_RECORD_THRESHOLD))
        review_threshold = clamp(item.get("review_threshold", DEFAULT_REVIEW_THRESHOLD))
        delete_threshold = clamp(item.get("delete_threshold", DEFAULT_DELETE_THRESHOLD))
        if not delete_threshold < record_threshold <= review_threshold:
            raise ValueError("Thresholds must satisfy delete < record <= review.")

        with self.connect() as con:
            con.execute(
                """
                INSERT INTO memory_traces (
                    id, conversation_id, turn_id, trace_stage, candidate_memory_type,
                    title, content, keywords_json, acquisition_mode, epistemic_status,
                    epistemic_confidence, activation, salience, stability,
                    continuity_score, retention_score, affect_signal_json,
                    evidence_summary, source_event_ids_json, source_json,
                    observation_statement, perspective, evidence_kind,
                    observation_fidelity, source_reliability, world_hypothesis,
                    record_threshold, review_threshold, delete_threshold, status,
                    last_recalled_at, recall_count, last_decayed_at, expires_at,
                    consolidated_at, consolidated_memory_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trace_id,
                    item.get("conversation_id"),
                    item.get("turn_id"),
                    trace_stage,
                    candidate_memory_type,
                    str(item.get("title") or content[:80]),
                    content,
                    dumps(item.get("keywords") or query_terms(content)),
                    acquisition_mode,
                    epistemic_status,
                    clamp(item.get("epistemic_confidence", 0.5)),
                    activation,
                    salience,
                    stability,
                    continuity,
                    score,
                    dumps(item.get("affect_signal") or {}),
                    str(item.get("evidence_summary") or ""),
                    dumps(item.get("source_event_ids") or []),
                    dumps(item.get("source") or {}),
                    item.get("observation_statement"),
                    item.get("perspective"),
                    item.get("evidence_kind"),
                    _as_optional_score(item.get("observation_fidelity")),
                    _as_optional_score(item.get("source_reliability")),
                    item.get("world_hypothesis"),
                    record_threshold,
                    review_threshold,
                    delete_threshold,
                    str(item.get("status", "active")),
                    item.get("last_recalled_at"),
                    int(item.get("recall_count", 0)),
                    item.get("last_decayed_at") or now,
                    item.get("expires_at"),
                    item.get("consolidated_at"),
                    item.get("consolidated_memory_id"),
                    item.get("created_at") or now,
                    now,
                ),
            )
            self.index_trace(con, trace_id)
        return self.get_trace(trace_id)

    def get_trace(self, trace_id: str, con: sqlite3.Connection | None = None) -> dict[str, Any]:
        own = con is None
        con = con or self.connect()
        try:
            row = con.execute("SELECT * FROM memory_traces WHERE id=?", (trace_id,)).fetchone()
            if row is None:
                raise KeyError(f"Memory trace not found: {trace_id}")
            return self.trace_row(row)
        finally:
            if own:
                con.close()

    def list_traces(
        self,
        status: str | None = None,
        conversation_id: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            if status not in TRACE_STATUSES:
                raise ValueError(f"Unknown trace status: {status}")
            clauses.append("status=?")
            params.append(status)
        elif not include_archived:
            clauses.append("status!='archived'")
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM memory_traces {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self.trace_row(row) for row in rows]

    def patch_trace(self, trace_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "trace_stage",
            "candidate_memory_type",
            "title",
            "content",
            "keywords",
            "acquisition_mode",
            "epistemic_status",
            "epistemic_confidence",
            "activation",
            "salience",
            "stability",
            "continuity_score",
            "affect_signal",
            "evidence_summary",
            "source_event_ids",
            "source",
            "observation_statement",
            "perspective",
            "evidence_kind",
            "observation_fidelity",
            "source_reliability",
            "world_hypothesis",
            "record_threshold",
            "review_threshold",
            "delete_threshold",
            "status",
            "expires_at",
        }
        updates = {key: value for key, value in patch.items() if key in allowed}
        if not updates:
            raise ValueError("Patch has no editable fields.")
        trace = self.get_trace(trace_id)
        merged = {**trace, **updates}
        if merged.get("candidate_memory_type") is not None and merged["candidate_memory_type"] not in MEMORY_TYPES:
            raise ValueError(f"Unknown candidate_memory_type: {merged['candidate_memory_type']}")
        if merged["trace_stage"] not in TRACE_STAGES or merged["status"] not in TRACE_STATUSES:
            raise ValueError("Invalid trace_stage or status.")
        if merged["acquisition_mode"] not in ACQUISITION_MODES or merged["epistemic_status"] not in EPISTEMIC_STATUSES:
            raise ValueError("Invalid acquisition_mode or epistemic_status.")
        if not merged["delete_threshold"] < merged["record_threshold"] <= merged["review_threshold"]:
            raise ValueError("Thresholds must satisfy delete < record <= review.")

        activation = clamp(merged["activation"])
        salience = clamp(merged["salience"])
        stability = clamp(merged["stability"])
        continuity = clamp(merged["continuity_score"])
        db_values = {
            "trace_stage": merged["trace_stage"],
            "candidate_memory_type": merged.get("candidate_memory_type"),
            "title": str(merged["title"]),
            "content": str(merged["content"]),
            "keywords_json": dumps(merged.get("keywords") or []),
            "acquisition_mode": merged["acquisition_mode"],
            "epistemic_status": merged["epistemic_status"],
            "epistemic_confidence": clamp(merged["epistemic_confidence"]),
            "activation": activation,
            "salience": salience,
            "stability": stability,
            "continuity_score": continuity,
            "retention_score": retention_score(activation, salience, stability, continuity),
            "affect_signal_json": dumps(merged.get("affect_signal") or {}),
            "evidence_summary": str(merged.get("evidence_summary") or ""),
            "source_event_ids_json": dumps(merged.get("source_event_ids") or []),
            "source_json": dumps(merged.get("source") or {}),
            "observation_statement": merged.get("observation_statement"),
            "perspective": merged.get("perspective"),
            "evidence_kind": merged.get("evidence_kind"),
            "observation_fidelity": _as_optional_score(merged.get("observation_fidelity")),
            "source_reliability": _as_optional_score(merged.get("source_reliability")),
            "world_hypothesis": merged.get("world_hypothesis"),
            "record_threshold": clamp(merged["record_threshold"]),
            "review_threshold": clamp(merged["review_threshold"]),
            "delete_threshold": clamp(merged["delete_threshold"]),
            "status": merged["status"],
            "expires_at": merged.get("expires_at"),
            "updated_at": utc_now(),
        }
        assignments = ", ".join(f"{key}=?" for key in db_values)
        with self.connect() as con:
            con.execute(f"UPDATE memory_traces SET {assignments} WHERE id=?", (*db_values.values(), trace_id))
            self.index_trace(con, trace_id)
        return self.get_trace(trace_id)

    def recall_trace(self, trace_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.connect() as con:
            trace = self.get_trace(trace_id, con=con)
            previous = parse_time(trace.get("last_recalled_at"), parse_time(trace["created_at"]))
            spaced = (now - previous).total_seconds() >= 86400
            activation = clamp(trace["activation"] + DEFAULT_RECALL_BOOST * (1.0 - trace["activation"]))
            stability = clamp(trace["stability"] + (DEFAULT_SPACED_STABILITY_BOOST if spaced else 0.0))
            score = retention_score(activation, trace["salience"], stability, trace["continuity_score"])
            con.execute(
                """
                UPDATE memory_traces
                SET activation=?, stability=?, retention_score=?, last_recalled_at=?,
                    recall_count=recall_count+1, updated_at=?
                WHERE id=?
                """,
                (activation, stability, score, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), trace_id),
            )
        return self.get_trace(trace_id)

    def review_trace(
        self,
        trace_id: str,
        decision: str,
        memory_type: str | None = None,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"confirm", "keep", "archive"}:
            raise ValueError("decision must be confirm, keep, or archive.")

        trace = self.get_trace(trace_id)
        if trace["status"] == "consolidated":
            if decision != "confirm":
                raise ValueError("Consolidated traces can only repeat the confirm decision.")
            memory_id = trace.get("consolidated_memory_id")
            return {
                "decision": "confirm",
                "trace": trace,
                "memory": self.get_memory(memory_id) if memory_id else None,
            }

        evidence_summary = trace.get("evidence_summary", "")
        if notes:
            evidence_summary = "\n".join(part for part in (evidence_summary, notes.strip()) if part)

        if decision == "confirm":
            reviewed = self.patch_trace(
                trace_id,
                {
                    "trace_stage": "candidate",
                    "acquisition_mode": "reviewed",
                    "epistemic_status": "confirmed",
                    "status": "review",
                    "evidence_summary": evidence_summary,
                },
            )
            memory = self.consolidate_trace(
                trace_id,
                memory_type=memory_type,
                title=title,
                confirmed=True,
            )
            return {
                "decision": decision,
                "trace": self.get_trace(reviewed["id"]),
                "memory": memory,
            }

        status = "active" if decision == "keep" else "archived"
        reviewed = self.patch_trace(
            trace_id,
            {
                "trace_stage": "candidate",
                "acquisition_mode": "reviewed",
                "status": status,
                "evidence_summary": evidence_summary,
            },
        )
        return {"decision": decision, "trace": reviewed, "memory": None}

    def consolidate_trace(
        self,
        trace_id: str,
        memory_type: str | None = None,
        title: str | None = None,
        confirmed: bool = False,
        con: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        own = con is None
        con = con or self.connect()
        try:
            trace = self.get_trace(trace_id, con=con)
            if trace["status"] == "consolidated" and trace.get("consolidated_memory_id"):
                return self.get_memory(trace["consolidated_memory_id"], con=con)
            if trace["status"] == "archived":
                raise ValueError("Archived traces cannot be consolidated.")
            target_type = memory_type or trace.get("candidate_memory_type")
            if target_type not in MEMORY_TYPES:
                raise ValueError("A valid memory_type is required for consolidation.")

            is_confirmed = confirmed or trace["acquisition_mode"] == "user_explicit" or trace["epistemic_status"] == "confirmed"
            now = utc_now()
            memory_id = new_id("memory")
            memory_item = {
                "id": memory_id,
                "memory_type": target_type,
                "title": title or trace["title"],
                "content": trace["content"],
                "keywords": trace["keywords"],
                "acquisition_mode": "reviewed" if confirmed and trace["acquisition_mode"] != "user_explicit" else trace["acquisition_mode"],
                "epistemic_status": "confirmed" if is_confirmed else "inferred",
                "epistemic_confidence": 1.0 if trace["acquisition_mode"] == "user_explicit" else trace["epistemic_confidence"],
                "activation": trace["activation"],
                "salience": trace["salience"],
                "stability": max(trace["stability"], 0.35 if not is_confirmed else 0.9),
                "continuity_score": trace["continuity_score"],
                "pinned": is_confirmed,
                "evidence_summary": trace["evidence_summary"],
                "source_event_ids": trace["source_event_ids"],
                "source": trace["source"],
                "metadata": {"origin_trace_id": trace_id, "affect_signal": trace["affect_signal"]},
                "observation_statement": trace.get("observation_statement"),
                "perspective": trace.get("perspective"),
                "evidence_kind": trace.get("evidence_kind"),
                "observation_fidelity": trace.get("observation_fidelity"),
                "source_reliability": trace.get("source_reliability"),
                "world_hypothesis": trace.get("world_hypothesis"),
                "consolidated_at": now,
            }
            self.upsert_memory(memory_item, con=con)
            con.execute(
                "INSERT INTO memory_evidence_links(id, memory_id, trace_id, relation, created_at) VALUES (?,?,?,?,?)",
                (new_id("evidence"), memory_id, trace_id, "consolidated_from", now),
            )
            for source_event_id in trace["source_event_ids"]:
                con.execute(
                    "INSERT INTO memory_evidence_links(id, memory_id, source_event_id, relation, created_at) VALUES (?,?,?,?,?)",
                    (new_id("evidence"), memory_id, source_event_id, "supported_by", now),
                )
            con.execute(
                """
                UPDATE memory_traces
                SET status='consolidated', consolidated_at=?, consolidated_memory_id=?, updated_at=?
                WHERE id=?
                """,
                (now, memory_id, now, trace_id),
            )
            con.execute("DELETE FROM memory_trace_fts WHERE trace_id=?", (trace_id,))
            if own:
                con.commit()
            return self.get_memory(memory_id, con=con)
        finally:
            if own:
                con.close()

    def run_maintenance(
        self,
        as_of: str | None = None,
        daily_decay_rate: float = DEFAULT_DAILY_DECAY_RATE,
        auto_consolidate: bool = False,
        archive_below_threshold: bool = True,
    ) -> dict[str, Any]:
        target_time = parse_time(as_of)
        target_text = target_time.isoformat(timespec="seconds")
        report: dict[str, Any] = {
            "as_of": target_text,
            "traces_decayed": 0,
            "traces_archived": 0,
            "traces_review": 0,
            "traces_consolidated": 0,
            "memories_decayed": 0,
            "memories_archived": 0,
            "consolidated_memory_ids": [],
        }
        rate = clamp(daily_decay_rate, 0.01, 0.999)
        with self.connect() as con:
            trace_rows = con.execute(
                "SELECT * FROM memory_traces WHERE status IN ('active','review')"
            ).fetchall()
            for row in trace_rows:
                trace = self.trace_row(row)
                previous = parse_time(trace["last_decayed_at"], parse_time(trace["created_at"]))
                elapsed_days = max(0.0, (target_time - previous).total_seconds() / 86400.0)
                if elapsed_days <= 0:
                    continue
                activation = decay_activation(trace["activation"], elapsed_days, trace["salience"], trace["stability"], rate)
                score = retention_score(activation, trace["salience"], trace["stability"], trace["continuity_score"])
                status = "active"
                expired = bool(trace.get("expires_at") and parse_time(trace["expires_at"]) <= target_time)
                if archive_below_threshold and (expired or score < trace["delete_threshold"]):
                    status = "archived"
                    report["traces_archived"] += 1
                elif score >= trace["review_threshold"]:
                    status = "review"
                    report["traces_review"] += 1
                con.execute(
                    "UPDATE memory_traces SET activation=?, retention_score=?, status=?, last_decayed_at=?, updated_at=? WHERE id=?",
                    (activation, score, status, target_text, target_text, trace["id"]),
                )
                report["traces_decayed"] += 1
                if status == "archived":
                    con.execute("DELETE FROM memory_trace_fts WHERE trace_id=?", (trace["id"],))
                elif auto_consolidate and score >= trace["record_threshold"] and trace.get("candidate_memory_type"):
                    memory = self.consolidate_trace(trace["id"], con=con)
                    report["traces_consolidated"] += 1
                    report["consolidated_memory_ids"].append(memory["id"])

            memory_rows = con.execute("SELECT * FROM memories WHERE archived=0").fetchall()
            for row in memory_rows:
                memory = self.memory_row(row)
                previous = parse_time(memory["last_decayed_at"], parse_time(memory["created_at"]))
                elapsed_days = max(0.0, (target_time - previous).total_seconds() / 86400.0)
                if elapsed_days <= 0:
                    continue
                activation = decay_activation(memory["activation"], elapsed_days, memory["salience"], memory["stability"], rate)
                score = retention_score(activation, memory["salience"], memory["stability"], memory["continuity_score"])
                archive = (
                    archive_below_threshold
                    and not memory["pinned"]
                    and memory["epistemic_status"] != "confirmed"
                    and score < DEFAULT_DELETE_THRESHOLD
                )
                con.execute(
                    "UPDATE memories SET activation=?, retention_score=?, last_decayed_at=?, archived=?, updated_at=? WHERE id=?",
                    (activation, score, target_text, 1 if archive else 0, target_text, memory["id"]),
                )
                report["memories_decayed"] += 1
                if archive:
                    report["memories_archived"] += 1
                    con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory["id"],))
        return report

    def get_memory(self, memory_id: str, con: sqlite3.Connection | None = None) -> dict[str, Any]:
        own = con is None
        con = con or self.connect()
        try:
            row = con.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(f"Long-term memory not found: {memory_id}")
            return self.memory_row(row)
        finally:
            if own:
                con.close()

    def list_evidence_links(self, memory_id: str) -> dict[str, Any]:
        with self.connect() as con:
            memory = self.get_memory(memory_id, con=con)
            rows = con.execute(
                """
                SELECT id, memory_id, trace_id, source_event_id, relation, created_at
                FROM memory_evidence_links
                WHERE memory_id=?
                ORDER BY created_at, id
                """,
                (memory_id,),
            ).fetchall()
        return {
            "memory_id": memory_id,
            "source_event_ids": memory["source_event_ids"],
            "links": [dict(row) for row in rows],
        }

    def list_memories(
        self,
        memory_type: str | None = None,
        epistemic_status: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if memory_type:
            if memory_type not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory_type: {memory_type}")
            clauses.append("memory_type=?")
            params.append(memory_type)
        if epistemic_status:
            if epistemic_status not in EPISTEMIC_STATUSES:
                raise ValueError(f"Unknown epistemic_status: {epistemic_status}")
            clauses.append("epistemic_status=?")
            params.append(epistemic_status)
        if not include_archived:
            clauses.append("archived=0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM memories {where} ORDER BY pinned DESC, retention_score DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self.memory_row(row) for row in rows]

    def patch_memory(self, memory_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "memory_type",
            "title",
            "content",
            "keywords",
            "entities",
            "acquisition_mode",
            "epistemic_status",
            "epistemic_confidence",
            "activation",
            "salience",
            "stability",
            "continuity_score",
            "pinned",
            "archived",
            "evidence_summary",
            "source_event_ids",
            "source",
            "metadata",
            "observation_statement",
            "perspective",
            "evidence_kind",
            "observation_fidelity",
            "source_reliability",
            "world_hypothesis",
            "expires_at",
        }
        updates = {key: value for key, value in patch.items() if key in allowed}
        if not updates:
            raise ValueError("Patch has no editable fields.")
        memory = self.get_memory(memory_id)
        merged = {**memory, **updates}
        if merged["memory_type"] not in MEMORY_TYPES:
            raise ValueError(f"Unknown memory_type: {merged['memory_type']}")
        if merged["acquisition_mode"] not in ACQUISITION_MODES or merged["epistemic_status"] not in EPISTEMIC_STATUSES:
            raise ValueError("Invalid acquisition_mode or epistemic_status.")

        activation = clamp(merged["activation"])
        salience = clamp(merged["salience"])
        stability = clamp(merged["stability"])
        continuity = clamp(merged["continuity_score"])
        values = {
            "memory_type": merged["memory_type"],
            "title": str(merged["title"]),
            "content": str(merged["content"]),
            "keywords_json": dumps(merged.get("keywords") or []),
            "entities_json": dumps(merged.get("entities") or []),
            "acquisition_mode": merged["acquisition_mode"],
            "epistemic_status": merged["epistemic_status"],
            "epistemic_confidence": clamp(merged["epistemic_confidence"]),
            "activation": activation,
            "salience": salience,
            "stability": stability,
            "continuity_score": continuity,
            "retention_score": retention_score(activation, salience, stability, continuity),
            "pinned": 1 if merged.get("pinned") else 0,
            "archived": 1 if merged.get("archived") else 0,
            "evidence_summary": str(merged.get("evidence_summary") or ""),
            "source_event_ids_json": dumps(merged.get("source_event_ids") or []),
            "source_json": dumps(merged.get("source") or {}),
            "metadata_json": dumps(merged.get("metadata") or {}),
            "observation_statement": merged.get("observation_statement"),
            "perspective": merged.get("perspective"),
            "evidence_kind": merged.get("evidence_kind"),
            "observation_fidelity": _as_optional_score(merged.get("observation_fidelity")),
            "source_reliability": _as_optional_score(merged.get("source_reliability")),
            "world_hypothesis": merged.get("world_hypothesis"),
            "expires_at": merged.get("expires_at"),
            "updated_at": utc_now(),
        }
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connect() as con:
            con.execute(f"UPDATE memories SET {assignments} WHERE id=?", (*values.values(), memory_id))
            if merged.get("archived"):
                con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory_id,))
            else:
                self.index_memory(con, memory_id)
        return self.get_memory(memory_id)

    def forget_memory(self, memory_id: str) -> None:
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not exists:
                raise KeyError(f"Long-term memory not found: {memory_id}")
            con.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory_id,))

    def forget_legacy_memory(self, legacy_type: str, legacy_id: str, con: sqlite3.Connection) -> None:
        rows = con.execute(
            "SELECT id FROM memories WHERE legacy_memory_type=? AND legacy_memory_id=?",
            (legacy_type, legacy_id),
        ).fetchall()
        for row in rows:
            con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (row["id"],))
            con.execute("DELETE FROM memories WHERE id=?", (row["id"],))

    def retrieve(
        self,
        query: str,
        limit: int = 8,
        include_archived: bool = False,
        memory_types: list[str] | None = None,
        update_recall: bool = True,
    ) -> list[dict[str, Any]]:
        types = self.normalize_memory_types(memory_types)
        terms = query_terms(query)
        query_normalized = normalize_text(query)
        results: list[dict[str, Any]] = []
        with self.connect() as con:
            fts_hits = self.fts_hits(con, terms)
            clauses = ["memory_type IN ({})".format(",".join("?" for _ in types))]
            params: list[Any] = list(types)
            if not include_archived:
                clauses.append("archived=0")
            rows = con.execute(f"SELECT * FROM memories WHERE {' AND '.join(clauses)}", params).fetchall()
            for row in rows:
                memory = self.memory_row(row)
                keyword = self.keyword_score(query_normalized, terms, memory, fts_hits)
                if keyword <= 0.0:
                    continue
                pinned_bonus = 1.0 if memory["pinned"] or memory["acquisition_mode"] == "user_explicit" else 0.0
                relevance = clamp(
                    0.55 * keyword
                    + 0.18 * memory["activation"]
                    + 0.12 * memory["salience"]
                    + 0.10 * memory["stability"]
                    + 0.05 * pinned_bonus
                )
                components = {
                    "keyword_score": keyword,
                    "activation": memory["activation"],
                    "salience": memory["salience"],
                    "stability": memory["stability"],
                    "retention_score": memory["retention_score"],
                    "pinned_or_explicit_bonus": pinned_bonus,
                }
                reasons = ["query matched title, content, or keywords"]
                if memory["activation"] >= 0.75:
                    reasons.append("memory is highly activated")
                if memory["pinned"]:
                    reasons.append("memory is pinned")
                if memory["acquisition_mode"] == "user_explicit":
                    reasons.append("memory was explicitly provided by the user")
                results.append(
                    {
                        "memory_type": memory["memory_type"],
                        "memory": memory,
                        "relevance": relevance,
                        "reason": "; ".join(reasons),
                        "inject_mode": self.inject_mode(relevance, memory),
                        "components": components,
                    }
                )
            results.sort(key=lambda item: item["relevance"], reverse=True)
            selected = results[: max(1, min(int(limit), 50))]
            if update_recall:
                now = datetime.now(timezone.utc)
                now_text = now.isoformat(timespec="seconds")
                for result in selected:
                    memory = result["memory"]
                    previous = parse_time(memory.get("last_recalled_at"), parse_time(memory["created_at"]))
                    spaced = (now - previous).total_seconds() >= 86400
                    activation = clamp(memory["activation"] + DEFAULT_RECALL_BOOST * (1.0 - memory["activation"]))
                    stability = clamp(memory["stability"] + (DEFAULT_SPACED_STABILITY_BOOST if spaced else 0.0))
                    score = retention_score(activation, memory["salience"], stability, memory["continuity_score"])
                    con.execute(
                        """
                        UPDATE memories
                        SET activation=?, stability=?, retention_score=?, last_recalled_at=?,
                            recall_count=recall_count+1, updated_at=?
                        WHERE id=?
                        """,
                        (activation, stability, score, now_text, now_text, memory["id"]),
                    )
                    con.execute(
                        """
                        INSERT INTO recall_history
                        (id, memory_id, memory_type, query, reason, relevance, inject_mode, created_at)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            new_id("recall"),
                            memory["id"],
                            memory["memory_type"],
                            query,
                            result["reason"],
                            result["relevance"],
                            result["inject_mode"],
                            now_text,
                        ),
                    )
                    memory["activation"] = activation
                    memory["stability"] = stability
                    memory["retention_score"] = score
                    memory["last_recalled_at"] = now_text
                    memory["recall_count"] += 1
        return selected

    @staticmethod
    def normalize_memory_types(memory_types: list[str] | None) -> list[str]:
        if not memory_types:
            return list(MEMORY_TYPES)
        normalized: list[str] = []
        for memory_type in memory_types:
            canonical = LEGACY_TYPE_MAP.get(memory_type, memory_type)
            if canonical not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory_type: {memory_type}")
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    def keyword_score(
        self,
        query_normalized: str,
        terms: list[str],
        memory: dict[str, Any],
        fts_hits: dict[str, float],
    ) -> float:
        title = normalize_text(memory["title"])
        content = normalize_text(memory["content"])
        keywords = [normalize_text(keyword) for keyword in memory["keywords"]]
        haystack = " ".join((title, content, " ".join(keywords)))
        score = 0.0
        if title and (title in query_normalized or query_normalized in title):
            score += 0.45
        if query_normalized and query_normalized in haystack:
            score += 0.45
        for keyword in keywords:
            if keyword and keyword in query_normalized:
                score += 0.35
        for term in terms:
            if term in title:
                score += 0.18
            elif term in haystack:
                score += 0.08
        score += 0.20 * fts_hits.get(memory["id"], 0.0)
        return clamp(score)

    @staticmethod
    def inject_mode(relevance: float, memory: dict[str, Any]) -> str:
        if relevance >= 0.78:
            return "full" if len(memory["content"]) < 500 else "short"
        if relevance >= 0.45:
            return "short"
        if relevance >= 0.25:
            return "reference_only"
        return "silent"

    def fts_hits(self, con: sqlite3.Connection, terms: list[str]) -> dict[str, float]:
        safe_terms = [term for term in terms if re.fullmatch(r"[a-z0-9_./:-]{2,}", term)]
        if not safe_terms:
            return {}
        expr = " OR ".join(f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in safe_terms[:8])
        rows = con.execute(
            "SELECT memory_id, rank FROM long_term_memory_fts WHERE long_term_memory_fts MATCH ? ORDER BY rank LIMIT 50",
            (expr,),
        ).fetchall()
        return {row["memory_id"]: 1.0 / (1.0 + abs(float(row["rank"]))) for row in rows}

    def index_trace(self, con: sqlite3.Connection, trace_id: str) -> None:
        row = con.execute("SELECT * FROM memory_traces WHERE id=?", (trace_id,)).fetchone()
        con.execute("DELETE FROM memory_trace_fts WHERE trace_id=?", (trace_id,))
        if row is None or row["status"] in {"archived", "consolidated"}:
            return
        con.execute(
            "INSERT INTO memory_trace_fts(trace_id, title, content, keywords) VALUES (?,?,?,?)",
            (trace_id, row["title"], row["content"], " ".join(loads(row["keywords_json"], []))),
        )

    def index_memory(self, con: sqlite3.Connection, memory_id: str) -> None:
        row = con.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        con.execute("DELETE FROM long_term_memory_fts WHERE memory_id=?", (memory_id,))
        if row is None or row["archived"]:
            return
        con.execute(
            "INSERT INTO long_term_memory_fts(memory_id, memory_type, title, content, keywords) VALUES (?,?,?,?,?)",
            (memory_id, row["memory_type"], row["title"], row["content"], " ".join(loads(row["keywords_json"], []))),
        )

    @staticmethod
    def trace_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "trace_stage": row["trace_stage"],
            "candidate_memory_type": row["candidate_memory_type"],
            "title": row["title"],
            "content": row["content"],
            "keywords": loads(row["keywords_json"], []),
            "acquisition_mode": row["acquisition_mode"],
            "epistemic_status": row["epistemic_status"],
            "epistemic_confidence": row["epistemic_confidence"],
            "activation": row["activation"],
            "salience": row["salience"],
            "stability": row["stability"],
            "continuity_score": row["continuity_score"],
            "retention_score": row["retention_score"],
            "affect_signal": loads(row["affect_signal_json"], {}),
            "evidence_summary": row["evidence_summary"],
            "source_event_ids": loads(row["source_event_ids_json"], []),
            "source": loads(row["source_json"], {}),
            "observation_statement": row["observation_statement"],
            "perspective": row["perspective"],
            "evidence_kind": row["evidence_kind"],
            "observation_fidelity": row["observation_fidelity"],
            "source_reliability": row["source_reliability"],
            "world_hypothesis": row["world_hypothesis"],
            "record_threshold": row["record_threshold"],
            "review_threshold": row["review_threshold"],
            "delete_threshold": row["delete_threshold"],
            "status": row["status"],
            "last_recalled_at": row["last_recalled_at"],
            "recall_count": row["recall_count"],
            "last_decayed_at": row["last_decayed_at"],
            "expires_at": row["expires_at"],
            "consolidated_at": row["consolidated_at"],
            "consolidated_memory_id": row["consolidated_memory_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def memory_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "memory_type": row["memory_type"],
            "title": row["title"],
            "content": row["content"],
            "keywords": loads(row["keywords_json"], []),
            "entities": loads(row["entities_json"], []),
            "acquisition_mode": row["acquisition_mode"],
            "epistemic_status": row["epistemic_status"],
            "epistemic_confidence": row["epistemic_confidence"],
            "activation": row["activation"],
            "salience": row["salience"],
            "stability": row["stability"],
            "continuity_score": row["continuity_score"],
            "retention_score": row["retention_score"],
            "last_recalled_at": row["last_recalled_at"],
            "recall_count": row["recall_count"],
            "last_decayed_at": row["last_decayed_at"],
            "expires_at": row["expires_at"],
            "pinned": bool(row["pinned"]),
            "archived": bool(row["archived"]),
            "evidence_summary": row["evidence_summary"],
            "source_event_ids": loads(row["source_event_ids_json"], []),
            "source": loads(row["source_json"], {}),
            "metadata": loads(row["metadata_json"], {}),
            "observation_statement": row["observation_statement"],
            "perspective": row["perspective"],
            "evidence_kind": row["evidence_kind"],
            "observation_fidelity": row["observation_fidelity"],
            "source_reliability": row["source_reliability"],
            "world_hypothesis": row["world_hypothesis"],
            "consolidated_at": row["consolidated_at"],
            "legacy_memory_type": row["legacy_memory_type"],
            "legacy_memory_id": row["legacy_memory_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def stats(self) -> dict[str, int]:
        with self.connect() as con:
            return {
                "memory_traces": con.execute("SELECT count(*) FROM memory_traces").fetchone()[0],
                "active_memory_traces": con.execute(
                    "SELECT count(*) FROM memory_traces WHERE status IN ('active','review')"
                ).fetchone()[0],
                "long_term_memories": con.execute("SELECT count(*) FROM memories").fetchone()[0],
                "active_long_term_memories": con.execute("SELECT count(*) FROM memories WHERE archived=0").fetchone()[0],
            }

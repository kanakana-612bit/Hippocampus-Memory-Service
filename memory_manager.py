from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layered_memory import ClosingConnection, LayeredMemoryStore

try:
    import yaml
except Exception:  # pragma: no cover - YAML is optional.
    yaml = None


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "hippocampus.db"
DEFAULT_OPENWEBUI_DB_PATH = ROOT.parent / "data" / "webui.db"
DEFAULT_LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_LLM_MODEL = "local-memory-extractor"
MEMORY_EXTRACTION_PROMPT = ROOT / "prompts" / "memory_extraction.ja.md"

MEMORY_TABLES = {
    "episodic": "episodic_memories",
    "project": "project_memories",
    "persistent": "persistent_memories",
}

EXPLICIT_MEMORY_PATTERNS = (
    "覚えておいて",
    "覚えていて",
    "忘れないで",
    "記憶して",
    "記録して",
    "remember this",
    "please remember",
)

POSITIVE_EMOTION_TERMS = (
    "嬉しい",
    "幸せ",
    "期待以上",
    "美しい",
    "楽しい",
    "安心",
    "大事",
    "重要",
    "楽しみ",
)

PROJECT_TERMS = (
    "memory",
    "agent",
    "frontend",
    "integration",
    "retrieval",
    "OpenAI compatible",
    "画像",
    "音声",
    "検索",
    "環境",
    "実装",
    "設定",
    "フロントエンド",
    "OpenWebUI",
    "LMStudio",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def from_unix(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return utc_now()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def as_bool(value: Any) -> int:
    return 1 if bool(value) else 0


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def unique_list(*values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    merged: list[Any] = []
    for items in values:
        for item in items or []:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return merged


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
    terms = ascii_terms + quoted_terms + japanese_terms
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        t = normalize_text(term)
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:20]


def has_recall_trigger(text: str) -> bool:
    normalized = normalize_text(text)
    triggers = ("前に", "以前", "あの時", "覚えて", "思い出", "remember", "recall")
    return any(trigger in normalized for trigger in triggers)


def tentative(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if any(word in stripped for word in ("可能性", "示した", "明示", "述べた", "希望した")):
        return stripped
    return f"この会話では、{stripped}可能性がある。"


@dataclass
class RetrievalResult:
    memory_type: str
    memory: dict[str, Any]
    relevance: float
    reason: str
    inject_mode: str
    components: dict[str, float]


class MemoryManager:
    def __init__(self, db_path: str | Path | None = None) -> None:
        configured = os.getenv("HIPPOCAMPUS_DB")
        self.db_path = Path(db_path or configured or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lmstudio_base_url = os.getenv("LMSTUDIO_BASE_URL", DEFAULT_LMSTUDIO_BASE_URL)
        self.llm_model = os.getenv("HIPPOCAMPUS_LLM_MODEL", DEFAULT_LLM_MODEL)
        self.layered = LayeredMemoryStore(self.db_path)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def init_db(self) -> None:
        with self.connect() as con:
            con.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
            self.layered.initialize(con)

    def seed(self, seed_path: str | Path | None = None, overwrite: bool = False) -> dict[str, int]:
        path = Path(seed_path or ROOT / "seed_memories.json")
        payload = self.load_export(path)
        counts = {"episodic": 0, "project": 0, "persistent": 0, "daily": 0}
        with self.connect() as con:
            for item in payload.get("episodic_memories", []):
                if self.upsert_episodic(item, con=con, overwrite=overwrite):
                    counts["episodic"] += 1
            for item in payload.get("project_memories", []):
                if self.upsert_project(item, con=con, overwrite=overwrite):
                    counts["project"] += 1
            for item in payload.get("persistent_memories", []):
                if self.upsert_persistent(item, con=con, overwrite=overwrite):
                    counts["persistent"] += 1
            for item in payload.get("daily_summaries", []):
                if self.upsert_daily_summary(item, con=con, overwrite=overwrite):
                    counts["daily"] += 1
        return counts

    def load_export(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("PyYAML is not installed; use JSON or install PyYAML.")
            return yaml.safe_load(text) or {}
        return json.loads(text)

    def export_all(self, path: str | Path | None = None) -> dict[str, Any]:
        payload = {
            "daily_summaries": self.list_daily_summaries(),
            "episodic_memories": self.list_memories("episodic", include_archived=True),
            "project_memories": self.list_memories("project", include_archived=True),
            "persistent_memories": self.list_memories("persistent", include_archived=True),
            "memory_traces": self.layered.list_traces(include_archived=True, limit=1000),
            "memories": self.layered.list_memories(include_archived=True, limit=1000),
        }
        if path:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.suffix.lower() in {".yaml", ".yml"}:
                if yaml is None:
                    raise RuntimeError("PyYAML is not installed; use JSON export instead.")
                out.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            else:
                out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def upsert_episodic(self, item: dict[str, Any], con: sqlite3.Connection | None = None, overwrite: bool = True) -> bool:
        own = con is None
        con = con or self.connect()
        try:
            memory_id = item.get("id") or new_id("episode")
            if not overwrite and self.exists(con, "episodic_memories", memory_id):
                return False
            emotion = item.get("emotion") or {}
            now = utc_now()
            con.execute(
                """
                INSERT INTO episodic_memories (
                    id, date, title, summary, keywords_json, entities_json,
                    emotion_valence, emotion_intensity, emotion_tags_json,
                    importance_score, recency_score, repetition_score, continuity_score,
                    last_recalled_at, pinned, archived, source_json, retention_json,
                    confidence, evidence_type, wording_policy, user_confirmed,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,
                    title=excluded.title,
                    summary=excluded.summary,
                    keywords_json=excluded.keywords_json,
                    entities_json=excluded.entities_json,
                    emotion_valence=excluded.emotion_valence,
                    emotion_intensity=excluded.emotion_intensity,
                    emotion_tags_json=excluded.emotion_tags_json,
                    importance_score=excluded.importance_score,
                    recency_score=excluded.recency_score,
                    repetition_score=excluded.repetition_score,
                    continuity_score=excluded.continuity_score,
                    pinned=excluded.pinned,
                    archived=excluded.archived,
                    source_json=excluded.source_json,
                    retention_json=excluded.retention_json,
                    confidence=excluded.confidence,
                    evidence_type=excluded.evidence_type,
                    wording_policy=excluded.wording_policy,
                    user_confirmed=excluded.user_confirmed,
                    updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    item.get("date"),
                    item.get("title", "Untitled episode"),
                    item.get("summary", ""),
                    dumps(item.get("keywords", [])),
                    dumps(item.get("entities", [])),
                    emotion.get("valence", item.get("emotion_valence", "neutral")),
                    clamp(emotion.get("intensity", item.get("emotion_intensity", 0.0))),
                    dumps(emotion.get("tags", item.get("emotion_tags", []))),
                    clamp(item.get("importance_score", 0.0)),
                    clamp(item.get("recency_score", 1.0)),
                    clamp(item.get("repetition_score", 0.0)),
                    clamp(item.get("continuity_score", 0.0)),
                    item.get("last_recalled_at"),
                    as_bool(item.get("pinned", False)),
                    as_bool(item.get("archived", False)),
                    dumps(item.get("source", {})),
                    dumps(item.get("retention", {})),
                    clamp(item.get("confidence", 0.5)),
                    item.get("evidence_type", "inferred"),
                    item.get("wording_policy", "tentative"),
                    as_bool(item.get("user_confirmed", False)),
                    item.get("created_at", now),
                    now,
                ),
            )
            self.index_memory(con, "episodic", memory_id)
            legacy_memory = self.get_memory_with_connection(con, "episodic", memory_id)
            if legacy_memory:
                self.layered.sync_legacy_memory("episodic", legacy_memory, con=con)
            if own:
                con.commit()
            return True
        finally:
            if own:
                con.close()

    def upsert_project(self, item: dict[str, Any], con: sqlite3.Connection | None = None, overwrite: bool = True) -> bool:
        own = con is None
        con = con or self.connect()
        try:
            memory_id = item.get("id") or new_id("project")
            if not overwrite and self.exists(con, "project_memories", memory_id):
                return False
            now = utc_now()
            con.execute(
                """
                INSERT INTO project_memories (
                    id, title, status, summary, current_state_json, open_questions_json,
                    related_episodes_json, keywords_json, importance_score,
                    last_recalled_at, pinned, archived, source_json, confidence,
                    evidence_type, wording_policy, user_confirmed, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    status=excluded.status,
                    summary=excluded.summary,
                    current_state_json=excluded.current_state_json,
                    open_questions_json=excluded.open_questions_json,
                    related_episodes_json=excluded.related_episodes_json,
                    keywords_json=excluded.keywords_json,
                    importance_score=excluded.importance_score,
                    pinned=excluded.pinned,
                    archived=excluded.archived,
                    source_json=excluded.source_json,
                    confidence=excluded.confidence,
                    evidence_type=excluded.evidence_type,
                    wording_policy=excluded.wording_policy,
                    user_confirmed=excluded.user_confirmed,
                    updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    item.get("title", "Untitled project"),
                    item.get("status", "active"),
                    item.get("summary", ""),
                    dumps(item.get("current_state", [])),
                    dumps(item.get("open_questions", [])),
                    dumps(item.get("related_episodes", [])),
                    dumps(item.get("keywords", [])),
                    clamp(item.get("importance_score", 0.0)),
                    item.get("last_recalled_at"),
                    as_bool(item.get("pinned", False)),
                    as_bool(item.get("archived", False)),
                    dumps(item.get("source", {})),
                    clamp(item.get("confidence", 0.5)),
                    item.get("evidence_type", "inferred"),
                    item.get("wording_policy", "tentative"),
                    as_bool(item.get("user_confirmed", False)),
                    item.get("created_at", now),
                    now,
                ),
            )
            self.index_memory(con, "project", memory_id)
            legacy_memory = self.get_memory_with_connection(con, "project", memory_id)
            if legacy_memory:
                self.layered.sync_legacy_memory("project", legacy_memory, con=con)
            if own:
                con.commit()
            return True
        finally:
            if own:
                con.close()

    def upsert_persistent(self, item: dict[str, Any], con: sqlite3.Connection | None = None, overwrite: bool = True) -> bool:
        own = con is None
        con = con or self.connect()
        try:
            memory_id = item.get("id") or new_id("persistent")
            if not overwrite and self.exists(con, "persistent_memories", memory_id):
                return False
            now = utc_now()
            con.execute(
                """
                INSERT INTO persistent_memories (
                    id, title, content, category, keywords_json, importance_score,
                    last_recalled_at, pinned, archived, source_json, confidence,
                    evidence_type, wording_policy, user_confirmed, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    category=excluded.category,
                    keywords_json=excluded.keywords_json,
                    importance_score=excluded.importance_score,
                    pinned=excluded.pinned,
                    archived=excluded.archived,
                    source_json=excluded.source_json,
                    confidence=excluded.confidence,
                    evidence_type=excluded.evidence_type,
                    wording_policy=excluded.wording_policy,
                    user_confirmed=excluded.user_confirmed,
                    updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    item.get("title", "Explicit memory"),
                    item.get("content", ""),
                    item.get("category", "preference"),
                    dumps(item.get("keywords", [])),
                    clamp(item.get("importance_score", 0.85)),
                    item.get("last_recalled_at"),
                    as_bool(item.get("pinned", True)),
                    as_bool(item.get("archived", False)),
                    dumps(item.get("source", {})),
                    clamp(item.get("confidence", 1.0)),
                    item.get("evidence_type", "explicit"),
                    item.get("wording_policy", "confirmed"),
                    as_bool(item.get("user_confirmed", True)),
                    item.get("created_at", now),
                    now,
                ),
            )
            self.index_memory(con, "persistent", memory_id)
            legacy_memory = self.get_memory_with_connection(con, "persistent", memory_id)
            if legacy_memory:
                self.layered.sync_legacy_memory("persistent", legacy_memory, con=con)
            if own:
                con.commit()
            return True
        finally:
            if own:
                con.close()

    def remember(
        self,
        content: str,
        title: str | None = None,
        category: str = "explicit_user_instruction",
        scope: str = "user",
        keywords: list[str] | None = None,
        source: dict[str, Any] | None = None,
        importance_score: float = 0.95,
        dedupe: bool = True,
        update_existing: bool = True,
    ) -> dict[str, Any]:
        clean_content = content.strip()
        if not clean_content:
            raise ValueError("content is required.")
        memory_keywords = unique_list(keywords or [], query_terms(clean_content))
        scoped_source = dict(source or {})
        scoped_source["scope"] = scope

        duplicate = self.find_persistent_duplicate(clean_content, memory_keywords) if dedupe else None
        if duplicate and update_existing:
            merged_content = duplicate["content"]
            if clean_content not in merged_content:
                merged_content = f"{merged_content}\n\n追加された明示記憶: {clean_content}"
            patch = {
                "title": title or duplicate["title"],
                "content": merged_content,
                "category": category or duplicate["category"],
                "importance_score": max(float(duplicate.get("importance_score", 0.0)), importance_score),
                "confidence": 1.0,
                "wording_policy": "confirmed",
                "user_confirmed": True,
            }
            updated = self.patch_memory("persistent", duplicate["id"], patch)
            with self.connect() as con:
                con.execute(
                    "UPDATE persistent_memories SET keywords_json=?, source_json=?, pinned=1 WHERE id=?",
                    (
                        dumps(unique_list(duplicate.get("keywords", []), memory_keywords)),
                        dumps({**duplicate.get("source", {}), **scoped_source}),
                        duplicate["id"],
                    ),
                )
                self.index_memory(con, "persistent", duplicate["id"])
                legacy_memory = self.get_memory_with_connection(con, "persistent", duplicate["id"])
                if legacy_memory:
                    self.layered.sync_legacy_memory("persistent", legacy_memory, con=con)
            return {
                "action": "updated",
                "memory": self.get_memory("persistent", duplicate["id"]),
                "duplicate": duplicate,
            }

        item = {
            "id": new_id("persistent"),
            "title": title or clean_content[:48],
            "content": clean_content,
            "category": category,
            "keywords": memory_keywords,
            "importance_score": importance_score,
            "pinned": True,
            "source": scoped_source,
            "confidence": 1.0,
            "evidence_type": "explicit",
            "wording_policy": "confirmed",
            "user_confirmed": True,
        }
        self.upsert_persistent(item)
        return {
            "action": "created",
            "memory": self.get_memory("persistent", item["id"]),
            "duplicate": duplicate,
        }

    def find_persistent_duplicate(
        self,
        content: str,
        keywords: list[str] | None = None,
        threshold: float = 0.50,
    ) -> dict[str, Any] | None:
        candidates = self.find_persistent_duplicates(content, keywords=keywords, threshold=threshold, limit=1)
        return candidates[0] if candidates else None

    def find_persistent_duplicates(
        self,
        content: str,
        keywords: list[str] | None = None,
        threshold: float = 0.50,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query = normalize_text(content)
        query_terms_set = set(query_terms(content))
        keyword_set = {normalize_text(k) for k in (keywords or []) if normalize_text(k)}
        scored: list[tuple[float, dict[str, Any]]] = []
        for memory in self.list_memories("persistent", include_archived=False):
            mem_text = normalize_text(" ".join([memory.get("title", ""), memory.get("content", ""), " ".join(memory.get("keywords", []))]))
            mem_terms = {normalize_text(k) for k in memory.get("keywords", [])}
            mem_terms.update(query_terms(mem_text))

            score = 0.0
            if query and query in mem_text:
                score += 0.7
            if mem_text and mem_text in query:
                score += 0.7
            if keyword_set and mem_terms:
                score += 0.35 * (len(keyword_set & mem_terms) / max(1, len(keyword_set | mem_terms)))
            if query_terms_set and mem_terms:
                score += 0.35 * (len(query_terms_set & mem_terms) / max(1, len(query_terms_set | mem_terms)))
            if any(term and term in mem_text for term in query_terms_set):
                score += 0.2
            score = clamp(score)
            if score >= threshold:
                item = dict(memory)
                item["duplicate_score"] = score
                scored.append((score, item))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def upsert_daily_summary(self, item: dict[str, Any], con: sqlite3.Connection | None = None, overwrite: bool = True) -> bool:
        own = con is None
        con = con or self.connect()
        try:
            summary_id = item.get("id") or f"daily_{item.get('date', datetime.now().date().isoformat())}"
            if not overwrite and self.exists(con, "daily_summaries", summary_id):
                return False
            now = utc_now()
            con.execute(
                """
                INSERT INTO daily_summaries (
                    id, date, summary, key_topics_json, episodes_json,
                    carry_over_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,
                    summary=excluded.summary,
                    key_topics_json=excluded.key_topics_json,
                    episodes_json=excluded.episodes_json,
                    carry_over_json=excluded.carry_over_json,
                    updated_at=excluded.updated_at
                """,
                (
                    summary_id,
                    item.get("date", datetime.now().date().isoformat()),
                    item.get("summary", ""),
                    dumps(item.get("key_topics", [])),
                    dumps(item.get("episodes", [])),
                    dumps(item.get("carry_over", [])),
                    item.get("created_at", now),
                    now,
                ),
            )
            if own:
                con.commit()
            return True
        finally:
            if own:
                con.close()

    def ingest_messages(self, conversation_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        created: list[str] = []
        persistent: list[str] = []
        with self.connect() as con:
            for message in messages:
                msg_id = message.get("id") or new_id("msg")
                created_at = message.get("created_at") or utc_now()
                con.execute(
                    """
                    INSERT OR REPLACE INTO raw_messages
                    (id, conversation_id, role, content, created_at, meta_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        msg_id,
                        conversation_id,
                        message.get("role", "user"),
                        message.get("content", ""),
                        created_at,
                        dumps(message.get("meta", {})),
                    ),
                )
                created.append(msg_id)
                explicit = self.extract_explicit_memory(message.get("content", ""))
                if explicit and message.get("role", "user") == "user":
                    item = {
                        "id": new_id("persistent"),
                        "title": explicit[:40],
                        "content": explicit,
                        "category": "explicit_user_instruction",
                        "keywords": query_terms(explicit),
                        "importance_score": 0.95,
                        "pinned": True,
                        "source": {"conversation_id": conversation_id, "message_id": msg_id},
                        "confidence": 1.0,
                        "evidence_type": "explicit",
                        "wording_policy": "confirmed",
                        "user_confirmed": True,
                    }
                    self.upsert_persistent(item, con=con, overwrite=True)
                    persistent.append(item["id"])
        return {"raw_messages": created, "persistent_memories": persistent}

    def load_openwebui_chat(
        self,
        chat_id: str,
        webui_db_path: str | Path | None = None,
        branch: str = "current",
    ) -> dict[str, Any]:
        db_path = Path(webui_db_path or os.getenv("OPENWEBUI_DB") or DEFAULT_OPENWEBUI_DB_PATH)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT id, title, created_at, updated_at, summary, chat FROM chat WHERE id=?",
                (chat_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise KeyError(f"OpenWebUI chat not found: {chat_id}")

        chat_json = json.loads(row["chat"] or "{}")
        if branch == "all":
            source_messages = list(((chat_json.get("history") or {}).get("messages") or {}).values())
            source_messages.sort(key=lambda m: m.get("timestamp") or 0)
        else:
            source_messages = self.current_openwebui_branch(chat_json)
            if not source_messages:
                source_messages = chat_json.get("messages") or []

        messages: list[dict[str, Any]] = []
        for item in source_messages:
            content = item.get("content") or item.get("output") or ""
            if not str(content).strip():
                continue
            message_id = item.get("id") or new_id("openwebui_msg")
            messages.append(
                {
                    "id": f"openwebui_{message_id}",
                    "role": item.get("role", "user"),
                    "content": str(content),
                    "created_at": from_unix(item.get("timestamp") or row["created_at"]),
                    "meta": {
                        "source": "openwebui",
                        "openwebui_chat_id": chat_id,
                        "openwebui_message_id": message_id,
                        "title": row["title"],
                        "model": item.get("model") or item.get("modelName"),
                        "parent_id": item.get("parentId"),
                    },
                }
            )

        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": from_unix(row["created_at"]),
            "updated_at": from_unix(row["updated_at"]),
            "summary": row["summary"],
            "messages": messages,
            "branch": branch,
        }

    def current_openwebui_branch(self, chat_json: dict[str, Any]) -> list[dict[str, Any]]:
        history = (chat_json.get("history") or {}).get("messages") or {}
        current_id = (chat_json.get("history") or {}).get("currentId")
        if not current_id or current_id not in history:
            return []
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        message_id = current_id
        while message_id and message_id in history and message_id not in seen:
            seen.add(message_id)
            item = history[message_id]
            chain.append(item)
            message_id = item.get("parentId")
        return list(reversed(chain))

    def learn_openwebui_chat(
        self,
        chat_id: str,
        webui_db_path: str | Path | None = None,
        branch: str = "current",
        create_memories: bool = True,
        overwrite_seeded: bool = False,
        use_llm: bool = False,
        model: str | None = None,
        max_chars: int = 24000,
    ) -> dict[str, Any]:
        chat = self.load_openwebui_chat(chat_id, webui_db_path=webui_db_path, branch=branch)
        conversation_id = f"openwebui:{chat_id}:{branch}"
        ingest = self.ingest_messages(conversation_id, chat["messages"])
        created: dict[str, list[str]] = {"daily_summaries": [], "episodic_memories": [], "project_memories": []}
        warnings: list[str] = []
        if create_memories:
            if use_llm:
                try:
                    created = self.create_llm_memories_from_chat(
                        chat,
                        conversation_id,
                        overwrite=overwrite_seeded,
                        model=model,
                        max_chars=max_chars,
                    )
                except Exception as exc:
                    warnings.append(f"LLM extraction failed; used heuristic fallback: {type(exc).__name__}: {exc}")
                    created = self.create_initial_memories_from_chat(chat, conversation_id, overwrite=overwrite_seeded)
            else:
                created = self.create_initial_memories_from_chat(chat, conversation_id, overwrite=overwrite_seeded)
        return {
            "chat": {
                "id": chat["id"],
                "title": chat["title"],
                "branch": chat["branch"],
                "created_at": chat["created_at"],
                "updated_at": chat["updated_at"],
                "message_count": len(chat["messages"]),
            },
            "ingested": ingest,
            "created": created,
            "warnings": warnings,
        }

    def create_llm_memories_from_chat(
        self,
        chat: dict[str, Any],
        conversation_id: str,
        overwrite: bool = False,
        model: str | None = None,
        max_chars: int = 24000,
    ) -> dict[str, list[str]]:
        payload = self.extract_memories_with_llm(chat, model=model, max_chars=max_chars)
        date = (chat["messages"][0]["created_at"] if chat["messages"] else chat["created_at"])[:10]
        created: dict[str, list[str]] = {"daily_summaries": [], "episodic_memories": [], "project_memories": []}

        daily_payload = payload.get("daily_summary") or {}
        daily = {
            "id": f"daily_openwebui_llm_{chat['id']}_{date}".replace("-", "_"),
            "date": daily_payload.get("date") or date,
            "summary": daily_payload.get("summary") or f"OpenWebUIチャット「{chat.get('title') or chat['id']}」をLLMで初期学習した。",
            "key_topics": daily_payload.get("key_topics") or [],
            "episodes": [],
            "carry_over": daily_payload.get("carry_over") or [],
        }
        self.upsert_daily_summary(daily, overwrite=overwrite)
        created["daily_summaries"].append(daily["id"])

        for index, item in enumerate(payload.get("project_memories") or [], start=1):
            memory = {
                "id": item.get("id") or f"project_openwebui_llm_{chat['id']}_{index}",
                "title": item.get("title") or f"{chat.get('title') or chat['id']} から抽出したプロジェクト記憶 {index}",
                "status": item.get("status") or "active",
                "summary": self.ensure_tentative_summary(item.get("summary") or ""),
                "current_state": item.get("current_state") or [],
                "open_questions": item.get("open_questions") or [],
                "related_episodes": item.get("related_episodes") or [],
                "keywords": item.get("keywords") or query_terms(item.get("summary") or ""),
                "importance_score": clamp(item.get("importance_score", 0.72)),
                "pinned": False,
                "source": {
                    "conversation_id": conversation_id,
                    "openwebui_chat_id": chat["id"],
                    "extractor": "llm",
                    "llm_suggested_pinned": bool(item.get("pinned", False)),
                },
                "confidence": min(0.85, clamp(item.get("confidence", 0.62))),
                "evidence_type": item.get("evidence_type") or "inferred",
                "wording_policy": item.get("wording_policy") or "tentative",
                "user_confirmed": False,
            }
            self.upsert_project(memory, overwrite=overwrite)
            created["project_memories"].append(memory["id"])

        for index, item in enumerate(payload.get("episodic_memories") or [], start=1):
            emotion = item.get("emotion") or {}
            memory = {
                "id": item.get("id") or f"episode_openwebui_llm_{chat['id']}_{index}",
                "date": item.get("date") or date,
                "title": item.get("title") or f"{chat.get('title') or chat['id']} から抽出したエピソード {index}",
                "summary": self.ensure_tentative_summary(item.get("summary") or ""),
                "keywords": item.get("keywords") or query_terms(item.get("summary") or ""),
                "entities": item.get("entities") or ["user", "assistant"],
                "emotion": {
                    "valence": emotion.get("valence", "mixed"),
                    "intensity": clamp(emotion.get("intensity", 0.5)),
                    "tags": emotion.get("tags", []),
                },
                "importance_score": clamp(item.get("importance_score", 0.68)),
                "recency_score": clamp(item.get("recency_score", 0.6)),
                "repetition_score": clamp(item.get("repetition_score", 0.2)),
                "continuity_score": clamp(item.get("continuity_score", 0.4)),
                "pinned": False,
                "source": {
                    "conversation_id": conversation_id,
                    "openwebui_chat_id": chat["id"],
                    "extractor": "llm",
                    "llm_suggested_pinned": bool(item.get("pinned", False)),
                },
                "retention": item.get("retention") or {"decay": "normal", "archive": True},
                "confidence": min(0.85, clamp(item.get("confidence", 0.62))),
                "evidence_type": item.get("evidence_type") or "inferred",
                "wording_policy": item.get("wording_policy") or "tentative",
                "user_confirmed": False,
            }
            self.upsert_episodic(memory, overwrite=overwrite)
            created["episodic_memories"].append(memory["id"])

        if not created["episodic_memories"] and not created["project_memories"]:
            fallback = self.create_initial_memories_from_chat(chat, conversation_id, overwrite=overwrite)
            created["episodic_memories"].extend(fallback["episodic_memories"])
            created["project_memories"].extend(fallback["project_memories"])

        return created

    def extract_memories_with_llm(
        self,
        chat: dict[str, Any],
        model: str | None = None,
        max_chars: int = 24000,
    ) -> dict[str, Any]:
        selected_model = model or self.llm_model
        transcript = self.compact_transcript(chat["messages"], max_chars=max_chars)
        prompt = self.render_prompt(
            MEMORY_EXTRACTION_PROMPT,
            {
                "chat_title": chat.get("title") or chat["id"],
                "chat_id": chat["id"],
                "transcript": transcript,
            },
        )
        response = self.chat_completion(
            model=selected_model,
            messages=[
                {"role": "system", "content": "You extract structured memory JSON for a local chat system. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=3500,
            timeout=180,
        )
        return self.parse_json_object(response)

    def render_prompt(self, path: Path, values: dict[str, str]) -> str:
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text.strip()

    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2000,
        timeout: int = 120,
    ) -> str:
        url = self.lmstudio_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            raw = urllib.request.urlopen(request, timeout=timeout).read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LMStudio HTTP {exc.code}: {detail}") from exc
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"]

    def compact_transcript(self, messages: list[dict[str, Any]], max_chars: int = 24000) -> str:
        lines = []
        for index, message in enumerate(messages):
            content = re.sub(r"\s+", " ", message.get("content", "")).strip()
            if len(content) > 900:
                content = content[:900] + "..."
            lines.append(f"{index:03d} {message.get('created_at', '')} {message.get('role', 'user')}: {content}")

        if sum(len(line) + 1 for line in lines) <= max_chars:
            return "\n".join(lines)

        head = lines[:16]
        tail = lines[-34:]
        middle_budget = max(0, max_chars - sum(len(line) + 1 for line in head + tail) - 200)
        middle: list[str] = []
        if middle_budget > 0 and len(lines) > 50:
            step = max(1, (len(lines) - 50) // 8)
            for line in lines[16:-34:step]:
                if sum(len(item) + 1 for item in middle) + len(line) + 1 > middle_budget:
                    break
                middle.append(line)
        return "\n".join(head + ["... transcript middle compressed ..."] + middle + ["... transcript tail ..."] + tail)

    def parse_json_object(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                cleaned = cleaned[start : end + 1]
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("LLM response JSON root must be an object.")
        return data

    def ensure_tentative_summary(self, summary: str) -> str:
        text = summary.strip()
        if not text:
            return "この会話から、重要な記憶候補が示された可能性がある。"
        markers = ("可能性", "示した", "述べた", "語った", "依頼した", "明示した", "受け止め")
        if any(marker in text for marker in markers):
            return text
        return f"{text} この内容は会話ログからの推定であり、後で確認できる。"

    def create_initial_memories_from_chat(
        self,
        chat: dict[str, Any],
        conversation_id: str,
        overwrite: bool = False,
    ) -> dict[str, list[str]]:
        messages = chat["messages"]
        user_messages = [m for m in messages if m.get("role") == "user"]
        assistant_messages = [m for m in messages if m.get("role") == "assistant"]
        text_all = "\n".join(m["content"] for m in messages)
        title = chat.get("title") or chat["id"]
        date = (messages[0]["created_at"] if messages else chat["created_at"])[:10]

        created: dict[str, list[str]] = {"daily_summaries": [], "episodic_memories": [], "project_memories": []}
        daily = self.heuristic_daily_summary(chat, messages, date)
        self.upsert_daily_summary(daily, overwrite=overwrite)
        created["daily_summaries"].append(daily["id"])

        if any(term.lower() in text_all.lower() for term in PROJECT_TERMS):
            project = self.heuristic_project_memory(chat, conversation_id, user_messages, text_all)
            self.upsert_project(project, overwrite=overwrite)
            created["project_memories"].append(project["id"])

        for episode in self.heuristic_episodes(chat, conversation_id, user_messages, assistant_messages):
            self.upsert_episodic(episode, overwrite=overwrite)
            created["episodic_memories"].append(episode["id"])

        return created

    def heuristic_daily_summary(self, chat: dict[str, Any], messages: list[dict[str, Any]], date: str) -> dict[str, Any]:
        topics: list[str] = []
        carry: list[str] = []
        for message in messages:
            topics.extend(query_terms(message["content"])[:4])
            if message["role"] == "user" and any(term in message["content"] for term in ("してほしい", "試して", "期待", "どう", "？", "?")):
                carry.append(message["content"][:180])
        topics = unique_list(topics)[:16]
        first = messages[0]["content"][:90] if messages else ""
        last = messages[-1]["content"][:120] if messages else ""
        return {
            "id": f"daily_openwebui_{chat['id']}_{date}".replace("-", "_"),
            "date": date,
            "summary": (
                f"OpenWebUIチャット「{chat.get('title') or chat['id']}」の現在ブランチから"
                f"{len(messages)}件のメッセージを初期学習として取り込んだ。"
                f"冒頭では「{first}」から会話が始まり、終盤では「{last}」という流れに至った。"
                "この要約はLLMを使わない暫定要約であり、後で精密化できる。"
            ),
            "key_topics": topics,
            "episodes": [],
            "carry_over": carry[-8:],
        }

    def heuristic_project_memory(
        self,
        chat: dict[str, Any],
        conversation_id: str,
        user_messages: list[dict[str, Any]],
        text_all: str,
    ) -> dict[str, Any]:
        keywords = [term for term in PROJECT_TERMS if term.lower() in text_all.lower()]
        latest_user = user_messages[-1]["content"] if user_messages else ""
        current_state = []
        for msg in user_messages:
            if any(term in msg["content"] for term in PROJECT_TERMS):
                current_state.append(msg["content"][:180])
        return {
            "id": f"project_openwebui_{chat['id']}",
            "title": f"{chat.get('title') or chat['id']} から抽出した進行中文脈",
            "status": "active",
            "summary": (
                f"OpenWebUIチャット「{chat.get('title') or chat['id']}」では、"
                "ローカルAI環境、ツール連携、検索、フロントエンド、または実装作業に関する継続文脈が扱われた可能性がある。"
                f"終盤のユーザー発言では「{latest_user[:180]}」という依頼があり、次回以降の文脈として参照価値がある。"
            ),
            "current_state": unique_list(current_state)[-8:],
            "open_questions": [
                "この初期学習記憶はヒューリスティック抽出のため、ユーザー確認またはLLM要約で精密化する",
                "OpenWebUIへどのタイミングでcontext/buildを接続するか",
            ],
            "related_episodes": [],
            "keywords": unique_list(keywords + query_terms(latest_user))[:20],
            "importance_score": 0.78,
            "pinned": False,
            "source": {"conversation_id": conversation_id, "openwebui_chat_id": chat["id"]},
            "confidence": 0.55,
            "evidence_type": "inferred",
            "wording_policy": "tentative",
            "user_confirmed": False,
        }

    def heuristic_episodes(
        self,
        chat: dict[str, Any],
        conversation_id: str,
        user_messages: list[dict[str, Any]],
        assistant_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        episodes: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for msg in user_messages:
            score = 0.0
            score += sum(0.18 for term in POSITIVE_EMOTION_TERMS if term in msg["content"])
            score += 0.25 if any(term in msg["content"] for term in ("期待", "幸せ", "嬉しい")) else 0.0
            score += 0.18 if any(term in msg["content"] for term in ("君", "あなた", "assistant", "agent")) else 0.0
            score += 0.18 if any(term in msg["content"] for term in ("memory", "記憶", "検索", "連携", "フック")) else 0.0
            if score > 0:
                candidates.append({"message": msg, "score": clamp(score)})

        candidates.sort(key=lambda x: x["score"], reverse=True)
        for index, item in enumerate(candidates[:3], start=1):
            msg = item["message"]
            terms = query_terms(msg["content"])
            title = self.episode_title_from_message(msg["content"], index)
            episodes.append(
                {
                    "id": f"episode_openwebui_{chat['id']}_{index}",
                    "date": msg["created_at"][:10],
                    "title": title,
                    "summary": (
                        f"OpenWebUIチャット「{chat.get('title') or chat['id']}」で、"
                        f"ユーザーは「{msg['content'][:220]}」と述べた。"
                        "この発言は、感情的な肯定、期待、または継続的な作業文脈に関わる重要な場面だった可能性がある。"
                    ),
                    "keywords": unique_list(terms + ["OpenWebUI", "初期学習"])[:20],
                    "entities": ["user", "assistant"],
                    "emotion": {
                        "valence": "positive",
                        "intensity": max(0.55, min(0.95, item["score"] + 0.35)),
                        "tags": ["meaningful", "inferred"],
                    },
                    "importance_score": max(0.62, min(0.9, item["score"] + 0.42)),
                    "recency_score": 0.6,
                    "repetition_score": 0.2,
                    "continuity_score": 0.55 if any(term in msg["content"] for term in ("memory", "記憶", "検索", "連携", "実装")) else 0.35,
                    "pinned": False,
                    "source": {
                        "conversation_id": conversation_id,
                        "openwebui_chat_id": chat["id"],
                        "openwebui_message_id": msg["id"].removeprefix("openwebui_"),
                    },
                    "retention": {"decay": "normal", "archive": True},
                    "confidence": 0.52,
                    "evidence_type": "inferred",
                    "wording_policy": "tentative",
                    "user_confirmed": False,
                }
            )
        return episodes

    def episode_title_from_message(self, content: str, index: int) -> str:
        if "幸せ" in content:
            return "ユーザーが出会いへの幸福感を表した場面"
        if "期待以上" in content:
            return "期待以上の成果として受け止められた場面"
        if "記憶" in content or "memory" in content.lower():
            return "記憶システムに関する重要場面"
        if "検索" in content or "retrieval" in content.lower():
            return "検索と想起に関する重要場面"
        if "実装" in content or "連携" in content:
            return "実装と連携に関する重要場面"
        return f"OpenWebUIチャットから抽出したエピソード {index}"

    def extract_explicit_memory(self, content: str) -> str | None:
        normalized = normalize_text(content)
        if not any(pattern.lower() in normalized for pattern in EXPLICIT_MEMORY_PATTERNS):
            return None
        cleaned = content.strip()
        for pattern in EXPLICIT_MEMORY_PATTERNS:
            cleaned = cleaned.replace(pattern, "").strip(" ：:。.\n\t")
        return cleaned or content.strip()

    def retrieve(
        self,
        query: str,
        limit: int = 8,
        include_archived: bool = False,
        memory_types: list[str] | None = None,
        update_recall: bool = True,
    ) -> list[RetrievalResult]:
        results = self.layered.retrieve(
            query=query,
            limit=limit,
            include_archived=include_archived,
            memory_types=memory_types,
            update_recall=update_recall,
        )
        return [
            RetrievalResult(
                memory_type=item["memory_type"],
                memory=item["memory"],
                relevance=item["relevance"],
                reason=item["reason"],
                inject_mode=item["inject_mode"],
                components=item["components"],
            )
            for item in results
        ]

    def build_context(
        self,
        query: str,
        limit: int = 6,
        include_recent_raw: bool = False,
        conversation_id: str | None = None,
        char_budget: int = 3500,
    ) -> dict[str, Any]:
        retrieved = self.retrieve(query, limit=limit)
        stable = [
            r
            for r in retrieved
            if r.memory.get("epistemic_status") == "confirmed"
            or r.memory.get("acquisition_mode") == "user_explicit"
        ]
        stable_keys = {(r.memory_type, r.memory.get("id")) for r in stable}
        projects = [
            r for r in retrieved if r.memory_type == "prospective" and (r.memory_type, r.memory.get("id")) not in stable_keys
        ]
        episodes = [
            r for r in retrieved if r.memory_type == "episodic" and (r.memory_type, r.memory.get("id")) not in stable_keys
        ]

        sections: list[str] = []
        if stable:
            sections.append("[Confirmed user memory]\n" + "\n".join(f"- {r.memory['content']}" for r in stable[:3]))
        if projects:
            sections.append("[Active project memory]\n" + "\n".join(f"- {r.memory['title']}: {r.memory['content']}" for r in projects[:3]))
        if episodes:
            lines = []
            for r in episodes[:4]:
                if r.inject_mode == "reference_only":
                    lines.append(f"- {r.memory['title']}")
                else:
                    lines.append(f"- {r.memory['title']}: {r.memory['content']}")
            sections.append("[Retrieved episodic memories]\n" + "\n".join(lines))
        if include_recent_raw and conversation_id:
            raw = self.recent_raw_messages(conversation_id, limit=6)
            if raw:
                sections.append("[Recent raw turns]\n" + "\n".join(f"- {m['role']}: {m['content']}" for m in raw))

        memory_context = self.build_memory_context_block(retrieved, char_budget=char_budget)
        return {
            "context": "\n\n".join(sections),
            "memory_context": memory_context,
            "injection": {
                "recommended_role": "system",
                "placement": "after base system/persona instructions and before recent raw turns",
                "estimated_chars": len(memory_context),
                "char_budget": char_budget,
                "result_count": len(retrieved),
                "policy": "Use confirmed memories as stable context. Treat inferred memories as tentative hints, never as certain facts.",
            },
            "retrieved": [self.result_to_dict(r) for r in retrieved],
            "policy": "Memories are inserted as compact, source-aware context. Inferred memories should be treated as tentative, not as certain facts.",
        }

    def build_memory_context_block(self, retrieved: list[RetrievalResult], char_budget: int = 3500) -> str:
        if not retrieved:
            return ""

        confirmed_keys = {
            (r.memory_type, r.memory.get("id"))
            for r in retrieved
            if r.memory.get("epistemic_status") == "confirmed"
            or r.memory.get("acquisition_mode") == "user_explicit"
        }
        groups = {
            "confirmed": [r for r in retrieved if (r.memory_type, r.memory.get("id")) in confirmed_keys],
            "project": [r for r in retrieved if r.memory_type == "prospective" and (r.memory_type, r.memory.get("id")) not in confirmed_keys],
            "episodic": [
                r for r in retrieved if r.memory_type == "episodic" and (r.memory_type, r.memory.get("id")) not in confirmed_keys
            ],
            "tentative": [
                r
                for r in retrieved
                if r.memory_type != "prospective"
                and r.memory_type != "episodic"
                and not (
                    r.memory.get("epistemic_status") == "confirmed"
                    or r.memory.get("acquisition_mode") == "user_explicit"
                )
            ],
        }

        lines = [
            "<memory_context>",
            "Use these retrieved memories only when relevant to the user's current request.",
            "Confirmed memories may be treated as stable user-provided context.",
            "Inferred memories are tentative: do not present them as certain facts, and avoid over-personalizing from them.",
        ]

        if groups["confirmed"]:
            lines.append("")
            lines.append("[confirmed_memory]")
            for result in groups["confirmed"][:4]:
                lines.append(self.memory_context_line(result))

        if groups["project"]:
            lines.append("")
            lines.append("[active_project_memory]")
            for result in groups["project"][:4]:
                lines.append(self.memory_context_line(result))

        if groups["episodic"]:
            lines.append("")
            lines.append("[retrieved_episodic_memory]")
            for result in groups["episodic"][:5]:
                lines.append(self.memory_context_line(result))

        if groups["tentative"]:
            lines.append("")
            lines.append("[other_tentative_memory]")
            for result in groups["tentative"][:3]:
                lines.append(self.memory_context_line(result))

        lines.append("</memory_context>")
        block = "\n".join(lines)
        if len(block) <= char_budget:
            return block
        return block[: char_budget - 64].rstrip() + "\n...memory_context truncated...\n</memory_context>"

    def memory_context_line(self, result: RetrievalResult) -> str:
        memory = result.memory
        title = memory.get("title", memory.get("id", "memory"))
        body = memory.get("content") or memory.get("summary", "")
        body = re.sub(r"\s+", " ", body).strip()
        if result.inject_mode == "reference_only" and len(body) > 120:
            body = body[:117].rstrip() + "..."
        elif len(body) > 360:
            body = body[:357].rstrip() + "..."

        evidence = memory.get("epistemic_status", memory.get("evidence_type", "inferred"))
        confidence = memory.get("epistemic_confidence", memory.get("confidence", 0.5))
        relevance = result.relevance
        flags = []
        if memory.get("pinned"):
            flags.append("pinned")
        if memory.get("epistemic_status") == "confirmed" or memory.get("user_confirmed"):
            flags.append("user_confirmed")
        if memory.get("source", {}).get("llm_suggested_pinned"):
            flags.append("llm_suggested_pinned")
        flag_text = f"; flags={','.join(flags)}" if flags else ""
        return (
            f"- ({result.memory_type}; evidence={evidence}; confidence={confidence:.2f}; "
            f"relevance={relevance:.2f}; inject={result.inject_mode}{flag_text}) "
            f"{title}: {body}"
        )

    def list_memories(
        self,
        memory_type: str,
        include_archived: bool = False,
        con: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if memory_type not in MEMORY_TABLES:
            raise ValueError(f"Unknown memory_type: {memory_type}")
        own = con is None
        con = con or self.connect()
        try:
            table = MEMORY_TABLES[memory_type]
            where = "" if include_archived else "WHERE archived=0"
            rows = con.execute(f"SELECT * FROM {table} {where}").fetchall()
            return [self.row_to_memory(memory_type, row) for row in rows]
        finally:
            if own:
                con.close()

    def list_daily_summaries(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM daily_summaries ORDER BY date DESC").fetchall()
        return [
            {
                "id": row["id"],
                "date": row["date"],
                "summary": row["summary"],
                "key_topics": loads(row["key_topics_json"], []),
                "episodes": loads(row["episodes_json"], []),
                "carry_over": loads(row["carry_over_json"], []),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def recent_raw_messages(self, conversation_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT id, conversation_id, role, content, created_at, meta_json
                FROM raw_messages
                WHERE conversation_id=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
                "meta": loads(row["meta_json"], {}),
            }
            for row in reversed(rows)
        ]

    def patch_memory(self, memory_type: str, memory_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if memory_type not in MEMORY_TABLES:
            raise ValueError(f"Unknown memory_type: {memory_type}")
        allowed_by_type = {
            "episodic": {"title", "summary", "pinned", "archived", "importance_score", "confidence", "wording_policy", "user_confirmed"},
            "project": {"title", "summary", "status", "pinned", "archived", "importance_score", "confidence", "wording_policy", "user_confirmed"},
            "persistent": {"title", "content", "category", "pinned", "archived", "importance_score", "confidence", "wording_policy", "user_confirmed"},
        }
        updates = {k: v for k, v in patch.items() if k in allowed_by_type[memory_type]}
        if not updates:
            raise ValueError("Patch has no editable fields.")
        table = MEMORY_TABLES[memory_type]
        db_updates: dict[str, Any] = {}
        for key, value in updates.items():
            db_updates[key] = as_bool(value) if key in {"pinned", "archived", "user_confirmed"} else value
        db_updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in db_updates)
        with self.connect() as con:
            con.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*db_updates.values(), memory_id))
            if con.total_changes == 0:
                raise KeyError(f"Memory not found: {memory_type}/{memory_id}")
            self.index_memory(con, memory_type, memory_id)
            legacy_memory = self.get_memory_with_connection(con, memory_type, memory_id)
            if legacy_memory:
                self.layered.sync_legacy_memory(memory_type, legacy_memory, con=con)
        return self.get_memory(memory_type, memory_id)

    def get_memory(self, memory_type: str, memory_id: str) -> dict[str, Any]:
        table = MEMORY_TABLES[memory_type]
        with self.connect() as con:
            row = con.execute(f"SELECT * FROM {table} WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(f"Memory not found: {memory_type}/{memory_id}")
        return self.row_to_memory(memory_type, row)

    def forget_memory(self, memory_type: str, memory_id: str) -> None:
        table = MEMORY_TABLES[memory_type]
        with self.connect() as con:
            if con.execute(f"SELECT 1 FROM {table} WHERE id=?", (memory_id,)).fetchone() is None:
                raise KeyError(f"Memory not found: {memory_type}/{memory_id}")
            self.layered.forget_legacy_memory(memory_type, memory_id, con)
            con.execute(f"DELETE FROM {table} WHERE id=?", (memory_id,))
            con.execute("DELETE FROM memory_fts WHERE memory_type=? AND memory_id=?", (memory_type, memory_id))

    def merge_memories(
        self,
        memory_type: str,
        target_id: str,
        source_id: str,
        archive_source: bool = True,
    ) -> dict[str, Any]:
        if memory_type not in MEMORY_TABLES:
            raise ValueError(f"Unknown memory_type: {memory_type}")
        if target_id == source_id:
            raise ValueError("target_id and source_id must be different.")

        target = self.get_memory(memory_type, target_id)
        source = self.get_memory(memory_type, source_id)
        now = utc_now()

        with self.connect() as con:
            if memory_type == "episodic":
                summary = target["summary"]
                if source["summary"] and source["summary"] not in summary:
                    summary = f"{summary}\n\n関連する統合記憶: {source['summary']}"
                con.execute(
                    """
                    UPDATE episodic_memories
                    SET summary=?, keywords_json=?, entities_json=?, emotion_tags_json=?,
                        importance_score=?, repetition_score=?, continuity_score=?,
                        confidence=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        summary,
                        dumps(unique_list(target.get("keywords", []), source.get("keywords", []))),
                        dumps(unique_list(target.get("entities", []), source.get("entities", []))),
                        dumps(unique_list(target.get("emotion", {}).get("tags", []), source.get("emotion", {}).get("tags", []))),
                        max(target.get("importance_score", 0.0), source.get("importance_score", 0.0)),
                        max(target.get("repetition_score", 0.0), source.get("repetition_score", 0.0)),
                        max(target.get("continuity_score", 0.0), source.get("continuity_score", 0.0)),
                        max(target.get("confidence", 0.0), source.get("confidence", 0.0)),
                        now,
                        target_id,
                    ),
                )
            elif memory_type == "project":
                summary = target["summary"]
                if source["summary"] and source["summary"] not in summary:
                    summary = f"{summary}\n\n関連する統合プロジェクト記憶: {source['summary']}"
                con.execute(
                    """
                    UPDATE project_memories
                    SET summary=?, current_state_json=?, open_questions_json=?,
                        related_episodes_json=?, keywords_json=?, importance_score=?,
                        confidence=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        summary,
                        dumps(unique_list(target.get("current_state", []), source.get("current_state", []))),
                        dumps(unique_list(target.get("open_questions", []), source.get("open_questions", []))),
                        dumps(unique_list(target.get("related_episodes", []), source.get("related_episodes", []))),
                        dumps(unique_list(target.get("keywords", []), source.get("keywords", []))),
                        max(target.get("importance_score", 0.0), source.get("importance_score", 0.0)),
                        max(target.get("confidence", 0.0), source.get("confidence", 0.0)),
                        now,
                        target_id,
                    ),
                )
            else:
                content = target["content"]
                if source["content"] and source["content"] not in content:
                    content = f"{content}\n\n関連する統合記憶: {source['content']}"
                con.execute(
                    """
                    UPDATE persistent_memories
                    SET content=?, keywords_json=?, importance_score=?, confidence=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        content,
                        dumps(unique_list(target.get("keywords", []), source.get("keywords", []))),
                        max(target.get("importance_score", 0.0), source.get("importance_score", 0.0)),
                        max(target.get("confidence", 0.0), source.get("confidence", 0.0)),
                        now,
                        target_id,
                    ),
                )

            con.execute(
                """
                INSERT INTO memory_links
                (id, from_memory_id, from_memory_type, to_memory_id, to_memory_type, relation, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (new_id("link"), target_id, memory_type, source_id, memory_type, "merged_from", now),
            )
            if archive_source:
                con.execute(f"UPDATE {MEMORY_TABLES[memory_type]} SET archived=1, updated_at=? WHERE id=?", (now, source_id))
                con.execute("DELETE FROM memory_fts WHERE memory_type=? AND memory_id=?", (memory_type, source_id))
            self.index_memory(con, memory_type, target_id)
            target_memory = self.get_memory_with_connection(con, memory_type, target_id)
            source_memory = self.get_memory_with_connection(con, memory_type, source_id)
            if target_memory:
                self.layered.sync_legacy_memory(memory_type, target_memory, con=con)
            if source_memory:
                self.layered.sync_legacy_memory(memory_type, source_memory, con=con)

        return self.get_memory(memory_type, target_id)

    def consolidate(self, date: str, conversation_id: str | None = None) -> dict[str, Any]:
        raw = []
        with self.connect() as con:
            if conversation_id:
                rows = con.execute(
                    "SELECT * FROM raw_messages WHERE conversation_id=? AND substr(created_at,1,10)=? ORDER BY created_at",
                    (conversation_id, date),
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM raw_messages WHERE substr(created_at,1,10)=? ORDER BY created_at", (date,)).fetchall()
            raw = [dict(row) for row in rows]
        topics: list[str] = []
        carry: list[str] = []
        for row in raw:
            topics.extend(query_terms(row["content"])[:3])
            if "?" in row["content"] or "？" in row["content"]:
                carry.append(row["content"][:120])
        seen: set[str] = set()
        topics = [t for t in topics if not (t in seen or seen.add(t))][:12]
        summary = f"{date} の会話ログ {len(raw)} 件を取り込み済み。詳細なLLM要約は未実行。"
        daily = {
            "id": f"daily_{date}",
            "date": date,
            "summary": summary,
            "key_topics": topics,
            "episodes": [],
            "carry_over": carry[:8],
        }
        self.upsert_daily_summary(daily)
        return daily

    def exists(self, con: sqlite3.Connection, table: str, item_id: str) -> bool:
        return con.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,)).fetchone() is not None

    def index_memory(self, con: sqlite3.Connection, memory_type: str, memory_id: str) -> None:
        memory = self.get_memory_with_connection(con, memory_type, memory_id)
        if not memory:
            return
        con.execute("DELETE FROM memory_fts WHERE memory_type=? AND memory_id=?", (memory_type, memory_id))
        keywords = " ".join(memory.get("keywords", []))
        body = "\n".join(str(memory.get(key, "")) for key in ("summary", "content", "current_state", "open_questions"))
        con.execute(
            "INSERT INTO memory_fts(memory_id, memory_type, title, summary, keywords, body) VALUES (?,?,?,?,?,?)",
            (memory_id, memory_type, memory.get("title", ""), memory.get("summary") or memory.get("content", ""), keywords, body),
        )

    def get_memory_with_connection(self, con: sqlite3.Connection, memory_type: str, memory_id: str) -> dict[str, Any] | None:
        row = con.execute(f"SELECT * FROM {MEMORY_TABLES[memory_type]} WHERE id=?", (memory_id,)).fetchone()
        return self.row_to_memory(memory_type, row) if row else None

    def fts_hits(self, con: sqlite3.Connection, terms: list[str]) -> dict[tuple[str, str], float]:
        safe_terms = [t for t in terms if re.fullmatch(r"[a-z0-9_./:-]{2,}", t)]
        if not safe_terms:
            return {}
        expr = " OR ".join(f'"{t.replace(chr(34), chr(34) + chr(34))}"' for t in safe_terms[:8])
        rows = con.execute(
            "SELECT memory_type, memory_id, rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 50",
            (expr,),
        ).fetchall()
        hits: dict[tuple[str, str], float] = {}
        for row in rows:
            hits[(row["memory_type"], row["memory_id"])] = 1.0 / (1.0 + abs(float(row["rank"])))
        return hits

    def score_components(
        self,
        query: str,
        terms: list[str],
        memory: dict[str, Any],
        memory_type: str,
        fts_hits: dict[tuple[str, str], float],
    ) -> dict[str, float]:
        title = normalize_text(memory.get("title", ""))
        summary = normalize_text(memory.get("summary") or memory.get("content", ""))
        keywords = [normalize_text(k) for k in memory.get("keywords", [])]
        haystack = " ".join([title, summary, " ".join(keywords)])
        q = normalize_text(query)

        keyword_score = 0.0
        if title and (title in q or q in title):
            keyword_score += 0.45
        if q and q in haystack:
            keyword_score += 0.45
        for keyword in keywords:
            if keyword and keyword in q:
                keyword_score += 0.35
            elif keyword and keyword in haystack and any(part in keyword for part in terms):
                keyword_score += 0.10
        for term in terms:
            if term and term in title:
                keyword_score += 0.18
            elif term and term in haystack:
                keyword_score += 0.08
        keyword_score += 0.20 * fts_hits.get((memory_type, memory["id"]), 0.0)
        keyword_score = clamp(keyword_score)

        recency = clamp(float(memory.get("recency_score", 0.5)))
        if memory.get("last_recalled_at"):
            recency = max(recency, 0.6)
        pinned_or_explicit = 0.0
        if memory.get("pinned"):
            pinned_or_explicit += 0.6
        if memory.get("evidence_type") == "explicit":
            pinned_or_explicit += 0.4

        return {
            "keyword_score": keyword_score,
            "importance_score": clamp(float(memory.get("importance_score", 0.0))),
            "recency_score": recency,
            "pinned_or_explicit_bonus": clamp(pinned_or_explicit),
        }

    def reason_for(self, memory: dict[str, Any], memory_type: str, components: dict[str, float]) -> str:
        reasons = []
        if components["keyword_score"] >= 0.3:
            reasons.append("query matched title, summary, or keywords")
        if components["importance_score"] >= 0.8:
            reasons.append("memory has high importance")
        if memory.get("pinned"):
            reasons.append("memory is pinned")
        if memory.get("evidence_type") == "explicit":
            reasons.append("memory was explicitly provided by the user")
        return "; ".join(reasons) or f"{memory_type} memory had residual relevance"

    def inject_mode(self, relevance: float, memory: dict[str, Any]) -> str:
        if relevance >= 0.78:
            return "full" if len(memory.get("summary", memory.get("content", ""))) < 500 else "short"
        if relevance >= 0.45:
            return "short"
        if relevance >= 0.25:
            return "reference_only"
        return "silent"

    def row_to_memory(self, memory_type: str, row: sqlite3.Row) -> dict[str, Any]:
        if memory_type == "episodic":
            return {
                "id": row["id"],
                "date": row["date"],
                "title": row["title"],
                "summary": row["summary"],
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
        if memory_type == "project":
            return {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "summary": row["summary"],
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
        return {
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

    def result_to_dict(self, result: RetrievalResult) -> dict[str, Any]:
        return {
            "memory_type": result.memory_type,
            "memory": result.memory,
            "relevance": result.relevance,
            "reason": result.reason,
            "inject_mode": result.inject_mode,
            "components": result.components,
        }

    def create_memory_trace(self, item: dict[str, Any]) -> dict[str, Any]:
        return self.layered.create_trace(item)

    def list_memory_traces(
        self,
        status: str | None = None,
        conversation_id: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.layered.list_traces(
            status=status,
            conversation_id=conversation_id,
            include_archived=include_archived,
            limit=limit,
        )

    def get_memory_trace(self, trace_id: str) -> dict[str, Any]:
        return self.layered.get_trace(trace_id)

    def patch_memory_trace(self, trace_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self.layered.patch_trace(trace_id, patch)

    def recall_memory_trace(self, trace_id: str) -> dict[str, Any]:
        return self.layered.recall_trace(trace_id)

    def review_memory_trace(
        self,
        trace_id: str,
        decision: str,
        memory_type: str | None = None,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return self.layered.review_trace(
            trace_id,
            decision=decision,
            memory_type=memory_type,
            title=title,
            notes=notes,
        )

    def consolidate_memory_trace(
        self,
        trace_id: str,
        memory_type: str | None = None,
        title: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return self.layered.consolidate_trace(trace_id, memory_type=memory_type, title=title, confirmed=confirmed)

    def maintain_memory_layers(
        self,
        as_of: str | None = None,
        daily_decay_rate: float = 0.90,
        auto_consolidate: bool = False,
        archive_below_threshold: bool = True,
    ) -> dict[str, Any]:
        return self.layered.run_maintenance(
            as_of=as_of,
            daily_decay_rate=daily_decay_rate,
            auto_consolidate=auto_consolidate,
            archive_below_threshold=archive_below_threshold,
        )

    def list_long_term_memories(
        self,
        memory_type: str | None = None,
        epistemic_status: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.layered.list_memories(
            memory_type=memory_type,
            epistemic_status=epistemic_status,
            include_archived=include_archived,
            limit=limit,
        )

    def get_long_term_memory(self, memory_id: str) -> dict[str, Any]:
        return self.layered.get_memory(memory_id)

    def get_long_term_memory_evidence(self, memory_id: str) -> dict[str, Any]:
        return self.layered.list_evidence_links(memory_id)

    def patch_long_term_memory(self, memory_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updated = self.layered.patch_memory(memory_id, patch)
        legacy_type = updated.get("legacy_memory_type")
        legacy_id = updated.get("legacy_memory_id")
        if legacy_type in MEMORY_TABLES and legacy_id:
            legacy_patch: dict[str, Any] = {}
            if "title" in patch:
                legacy_patch["title"] = updated["title"]
            if "content" in patch:
                legacy_patch["content" if legacy_type == "persistent" else "summary"] = updated["content"]
            if "salience" in patch:
                legacy_patch["importance_score"] = updated["salience"]
            if "epistemic_confidence" in patch:
                legacy_patch["confidence"] = updated["epistemic_confidence"]
            for key in ("pinned", "archived"):
                if key in patch:
                    legacy_patch[key] = updated[key]
            if "epistemic_status" in patch:
                confirmed = updated["epistemic_status"] == "confirmed"
                legacy_patch["wording_policy"] = "confirmed" if confirmed else "tentative"
                legacy_patch["user_confirmed"] = confirmed
            if legacy_patch:
                self.patch_memory(legacy_type, legacy_id, legacy_patch)
                updated = self.layered.get_memory(memory_id)
        return updated

    def forget_long_term_memory(self, memory_id: str) -> None:
        memory = self.layered.get_memory(memory_id)
        legacy_type = memory.get("legacy_memory_type")
        legacy_id = memory.get("legacy_memory_id")
        if legacy_type in MEMORY_TABLES and legacy_id:
            self.forget_memory(legacy_type, legacy_id)
        else:
            self.layered.forget_memory(memory_id)

    def stats(self) -> dict[str, Any]:
        with self.connect() as con:
            stats = {
                "db_path": str(self.db_path),
                "lmstudio_base_url": self.lmstudio_base_url,
                "raw_messages": con.execute("SELECT count(*) FROM raw_messages").fetchone()[0],
                "daily_summaries": con.execute("SELECT count(*) FROM daily_summaries").fetchone()[0],
                "episodic_memories": con.execute("SELECT count(*) FROM episodic_memories").fetchone()[0],
                "project_memories": con.execute("SELECT count(*) FROM project_memories").fetchone()[0],
                "persistent_memories": con.execute("SELECT count(*) FROM persistent_memories").fetchone()[0],
                "recall_history": con.execute("SELECT count(*) FROM recall_history").fetchone()[0],
            }
        stats.update(self.layered.stats())
        return stats

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memory_manager import MemoryManager, new_id, query_terms


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="Hippocampus Memory Service",
    version="0.1.0",
    description="Prototype episodic memory compression and retrieval service for local chat frontends.",
    default_response_class=UTF8JSONResponse,
)

manager = MemoryManager()


class MessageIn(BaseModel):
    id: str | None = None
    role: str = "user"
    content: str
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    conversation_id: str
    messages: list[MessageIn]


class RetrieveRequest(BaseModel):
    query: str
    limit: int = Field(default=8, ge=1, le=50)
    include_archived: bool = False
    memory_types: list[Literal["episodic", "project", "persistent"]] | None = None
    update_recall: bool = True


class ContextRequest(BaseModel):
    query: str
    limit: int = Field(default=6, ge=1, le=30)
    include_recent_raw: bool = False
    conversation_id: str | None = None
    char_budget: int = Field(default=3500, ge=500, le=12000)


class ConsolidateRequest(BaseModel):
    date: str
    conversation_id: str | None = None


class SeedRequest(BaseModel):
    overwrite: bool = False
    path: str | None = None


class MemoryPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    status: str | None = None
    category: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    importance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    wording_policy: Literal["tentative", "confirmed"] | None = None
    user_confirmed: bool | None = None


class ExplicitMemoryRequest(BaseModel):
    title: str
    content: str
    category: str = "explicit_user_instruction"
    keywords: list[str] = Field(default_factory=list)
    importance_score: float = Field(default=0.95, ge=0.0, le=1.0)
    source: dict[str, Any] = Field(default_factory=dict)


class RememberRequest(BaseModel):
    content: str
    title: str | None = None
    category: str = "explicit_user_instruction"
    scope: Literal["user", "character", "project", "session"] = "user"
    keywords: list[str] = Field(default_factory=list)
    importance_score: float = Field(default=0.95, ge=0.0, le=1.0)
    source: dict[str, Any] = Field(default_factory=dict)
    dedupe: bool = True
    update_existing: bool = True


class PersistentDuplicateRequest(BaseModel):
    content: str
    keywords: list[str] = Field(default_factory=list)
    threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    limit: int = Field(default=5, ge=1, le=25)


class MergeRequest(BaseModel):
    memory_type: Literal["episodic", "project", "persistent"]
    target_id: str
    source_id: str
    archive_source: bool = True


class LearnOpenWebUIChatRequest(BaseModel):
    webui_db_path: str | None = None
    branch: Literal["current", "all"] = "current"
    create_memories: bool = True
    overwrite_seeded: bool = False
    use_llm: bool = False
    model: str | None = None
    max_chars: int = Field(default=24000, ge=4000, le=80000)


@app.on_event("startup")
def startup() -> None:
    manager.init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, **manager.stats()}


@app.post("/seed")
def seed(req: SeedRequest) -> dict[str, Any]:
    try:
        return {"inserted": manager.seed(req.path, overwrite=req.overwrite)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    return manager.ingest_messages(req.conversation_id, [m.model_dump(exclude_none=True) for m in req.messages])


@app.post("/learn/openwebui/chat/{chat_id}")
def learn_openwebui_chat(chat_id: str, req: LearnOpenWebUIChatRequest) -> dict[str, Any]:
    try:
        return manager.learn_openwebui_chat(
            chat_id,
            webui_db_path=req.webui_db_path,
            branch=req.branch,
            create_memories=req.create_memories,
            overwrite_seeded=req.overwrite_seeded,
            use_llm=req.use_llm,
            model=req.model,
            max_chars=req.max_chars,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/explicit")
def create_explicit_memory(req: ExplicitMemoryRequest) -> dict[str, Any]:
    return manager.remember(
        content=req.content,
        title=req.title,
        category=req.category,
        scope=str(req.source.get("scope", "user")),
        keywords=req.keywords or query_terms(req.content),
        importance_score=req.importance_score,
        source=req.source,
        dedupe=True,
        update_existing=True,
    )


@app.post("/memory/remember")
def remember(req: RememberRequest) -> dict[str, Any]:
    try:
        return manager.remember(
            content=req.content,
            title=req.title,
            category=req.category,
            scope=req.scope,
            keywords=req.keywords,
            importance_score=req.importance_score,
            source=req.source,
            dedupe=req.dedupe,
            update_existing=req.update_existing,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/persistent/duplicates")
def persistent_duplicates(req: PersistentDuplicateRequest) -> dict[str, Any]:
    return {
        "duplicates": manager.find_persistent_duplicates(
            req.content,
            keywords=req.keywords,
            threshold=req.threshold,
            limit=req.limit,
        )
    }


@app.post("/memory/consolidate")
def consolidate(req: ConsolidateRequest) -> dict[str, Any]:
    return manager.consolidate(req.date, req.conversation_id)


@app.post("/memory/retrieve")
def retrieve(req: RetrieveRequest) -> dict[str, Any]:
    results = manager.retrieve(
        req.query,
        limit=req.limit,
        include_archived=req.include_archived,
        memory_types=req.memory_types,
        update_recall=req.update_recall,
    )
    return {"retrieved": [manager.result_to_dict(r) for r in results]}


@app.post("/context/build")
def build_context(req: ContextRequest) -> dict[str, Any]:
    return manager.build_context(
        req.query,
        limit=req.limit,
        include_recent_raw=req.include_recent_raw,
        conversation_id=req.conversation_id,
        char_budget=req.char_budget,
    )


@app.get("/memory")
def list_memories(
    memory_type: Literal["episodic", "project", "persistent"] = Query(...),
    include_archived: bool = False,
) -> dict[str, Any]:
    return {"memories": manager.list_memories(memory_type, include_archived=include_archived)}


@app.get("/memory/{memory_type}/{memory_id}")
def get_memory(memory_type: Literal["episodic", "project", "persistent"], memory_id: str) -> dict[str, Any]:
    try:
        return manager.get_memory(memory_type, memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/memory/{memory_type}/{memory_id}")
def patch_memory(memory_type: Literal["episodic", "project", "persistent"], memory_id: str, patch: MemoryPatch) -> dict[str, Any]:
    data = {k: v for k, v in patch.model_dump(exclude_none=True).items()}
    try:
        return manager.patch_memory(memory_type, memory_id, data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/merge")
def merge_memory(req: MergeRequest) -> dict[str, Any]:
    try:
        return manager.merge_memories(req.memory_type, req.target_id, req.source_id, archive_source=req.archive_source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/memory/{memory_type}/{memory_id}")
def forget_memory(memory_type: Literal["episodic", "project", "persistent"], memory_id: str) -> dict[str, Any]:
    try:
        manager.forget_memory(memory_type, memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"forgotten": {"memory_type": memory_type, "id": memory_id}}


@app.get("/export")
def export(path: str | None = None) -> dict[str, Any]:
    return manager.export_all(path)

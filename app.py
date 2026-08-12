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
    version="0.2.0",
    description="Layered short-term and long-term memory service for local chat frontends.",
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
    memory_types: list[
        Literal["episodic", "semantic", "prospective", "procedural", "embodied", "project", "persistent"]
    ] | None = None
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


class MemoryTraceCreate(BaseModel):
    conversation_id: str | None = None
    turn_id: str | None = None
    trace_stage: Literal["proto", "candidate"] = "proto"
    candidate_memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None
    title: str | None = None
    content: str
    keywords: list[str] = Field(default_factory=list)
    acquisition_mode: Literal["automatic", "user_explicit", "reviewed", "system_derived"] = "automatic"
    epistemic_status: Literal["observed", "inferred", "confirmed", "disputed"] = "inferred"
    epistemic_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    activation: float = Field(default=0.5, ge=0.0, le=1.0)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    stability: float = Field(default=0.1, ge=0.0, le=1.0)
    continuity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    affect_signal: dict[str, Any] = Field(default_factory=dict)
    evidence_summary: str = ""
    source_event_ids: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    observation_statement: str | None = None
    perspective: str | None = None
    evidence_kind: Literal["direct_measurement", "self_report", "model_output", "derived"] | None = None
    observation_fidelity: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reliability: float | None = Field(default=None, ge=0.0, le=1.0)
    world_hypothesis: str | None = None
    record_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    delete_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    expires_at: str | None = None


class MemoryTracePatch(BaseModel):
    trace_stage: Literal["proto", "candidate"] | None = None
    candidate_memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None
    title: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    acquisition_mode: Literal["automatic", "user_explicit", "reviewed", "system_derived"] | None = None
    epistemic_status: Literal["observed", "inferred", "confirmed", "disputed"] | None = None
    epistemic_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    activation: float | None = Field(default=None, ge=0.0, le=1.0)
    salience: float | None = Field(default=None, ge=0.0, le=1.0)
    stability: float | None = Field(default=None, ge=0.0, le=1.0)
    continuity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    affect_signal: dict[str, Any] | None = None
    evidence_summary: str | None = None
    source_event_ids: list[str] | None = None
    source: dict[str, Any] | None = None
    observation_statement: str | None = None
    perspective: str | None = None
    evidence_kind: Literal["direct_measurement", "self_report", "model_output", "derived"] | None = None
    observation_fidelity: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reliability: float | None = Field(default=None, ge=0.0, le=1.0)
    world_hypothesis: str | None = None
    record_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    review_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    delete_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "review", "consolidated", "archived"] | None = None
    expires_at: str | None = None


class TraceConsolidateRequest(BaseModel):
    memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None
    title: str | None = None
    confirmed: bool = False


class TraceReviewRequest(BaseModel):
    decision: Literal["confirm", "keep", "archive"]
    memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None
    title: str | None = None
    notes: str | None = None


class MemoryMaintenanceRequest(BaseModel):
    as_of: str | None = None
    daily_decay_rate: float = Field(default=0.90, ge=0.01, le=0.999)
    auto_consolidate: bool = False
    archive_below_threshold: bool = True


class LongTermMemoryPatch(BaseModel):
    memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None
    title: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    entities: list[str] | None = None
    acquisition_mode: Literal["automatic", "user_explicit", "reviewed", "system_derived"] | None = None
    epistemic_status: Literal["observed", "inferred", "confirmed", "disputed"] | None = None
    epistemic_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    activation: float | None = Field(default=None, ge=0.0, le=1.0)
    salience: float | None = Field(default=None, ge=0.0, le=1.0)
    stability: float | None = Field(default=None, ge=0.0, le=1.0)
    continuity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    pinned: bool | None = None
    archived: bool | None = None
    evidence_summary: str | None = None
    source_event_ids: list[str] | None = None
    source: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    observation_statement: str | None = None
    perspective: str | None = None
    evidence_kind: Literal["direct_measurement", "self_report", "model_output", "derived"] | None = None
    observation_fidelity: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reliability: float | None = Field(default=None, ge=0.0, le=1.0)
    world_hypothesis: str | None = None
    expires_at: str | None = None


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


@app.post("/memory/traces")
def create_memory_trace(req: MemoryTraceCreate) -> dict[str, Any]:
    try:
        return manager.create_memory_trace(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/memory/traces")
def list_memory_traces(
    status: Literal["active", "review", "consolidated", "archived"] | None = None,
    conversation_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return {
        "traces": manager.list_memory_traces(
            status=status,
            conversation_id=conversation_id,
            include_archived=include_archived,
            limit=limit,
        )
    }


@app.get("/memory/traces/{trace_id}")
def get_memory_trace(trace_id: str) -> dict[str, Any]:
    try:
        return manager.get_memory_trace(trace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/memory/traces/{trace_id}")
def patch_memory_trace(trace_id: str, patch: MemoryTracePatch) -> dict[str, Any]:
    try:
        return manager.patch_memory_trace(trace_id, patch.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/traces/{trace_id}/recall")
def recall_memory_trace(trace_id: str) -> dict[str, Any]:
    try:
        return manager.recall_memory_trace(trace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/memory/traces/{trace_id}/review")
def review_memory_trace(trace_id: str, req: TraceReviewRequest) -> dict[str, Any]:
    try:
        return manager.review_memory_trace(
            trace_id,
            decision=req.decision,
            memory_type=req.memory_type,
            title=req.title,
            notes=req.notes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/traces/{trace_id}/consolidate")
def consolidate_memory_trace(trace_id: str, req: TraceConsolidateRequest) -> dict[str, Any]:
    try:
        return manager.consolidate_memory_trace(
            trace_id,
            memory_type=req.memory_type,
            title=req.title,
            confirmed=req.confirmed,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/maintenance")
def maintain_memory_layers(req: MemoryMaintenanceRequest) -> dict[str, Any]:
    try:
        return manager.maintain_memory_layers(
            as_of=req.as_of,
            daily_decay_rate=req.daily_decay_rate,
            auto_consolidate=req.auto_consolidate,
            archive_below_threshold=req.archive_below_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memories/retrieve")
def retrieve_long_term_memories(req: RetrieveRequest) -> dict[str, Any]:
    try:
        results = manager.retrieve(
            req.query,
            limit=req.limit,
            include_archived=req.include_archived,
            memory_types=req.memory_types,
            update_recall=req.update_recall,
        )
        return {"retrieved": [manager.result_to_dict(result) for result in results]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/memories")
def list_long_term_memories(
    memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None,
    epistemic_status: Literal["observed", "inferred", "confirmed", "disputed"] | None = None,
    include_archived: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    return {
        "memories": manager.list_long_term_memories(
            memory_type=memory_type,
            epistemic_status=epistemic_status,
            include_archived=include_archived,
            limit=limit,
        )
    }


@app.get("/memories/{memory_id}")
def get_long_term_memory(memory_id: str) -> dict[str, Any]:
    try:
        return manager.get_long_term_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/memories/{memory_id}/evidence")
def get_long_term_memory_evidence(memory_id: str) -> dict[str, Any]:
    try:
        return manager.get_long_term_memory_evidence(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/memories/{memory_id}")
def patch_long_term_memory(memory_id: str, patch: LongTermMemoryPatch) -> dict[str, Any]:
    try:
        return manager.patch_long_term_memory(memory_id, patch.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/memories/{memory_id}")
def forget_long_term_memory(memory_id: str) -> dict[str, Any]:
    try:
        manager.forget_long_term_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"forgotten": {"memory_id": memory_id}}


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

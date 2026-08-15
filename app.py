from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memory_manager import MemoryManager, new_id, query_terms


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="Hippocampus Memory Service",
    version="0.8.0",
    description="Layered short-term and long-term memory service for local chat frontends.",
    default_response_class=UTF8JSONResponse,
)

manager = MemoryManager()


class MessageIn(BaseModel):
    id: str | None = None
    role: str = "user"
    content: str
    created_at: str | None = None
    event_time: str | None = None
    source_time: str | None = None
    timezone: str | None = None
    time_source: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    source_channel: str | None = None
    content_origin: Literal["original", "quoted", "summary", "inferred", "generated", "derived"] | None = None
    extractor: str | None = None
    derived_from: list[dict[str, str]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    conversation_id: str
    messages: list[MessageIn]
    auto_capture: bool = True
    capture_threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    default_timezone: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class RetrieveRequest(BaseModel):
    query: str
    limit: int = Field(default=8, ge=1, le=50)
    include_archived: bool = False
    memory_types: list[
        Literal["episodic", "semantic", "prospective", "procedural", "embodied", "project", "persistent"]
    ] | None = None
    update_recall: bool = True
    temporal_scope: Literal["auto", "current", "historical", "future", "all"] = "auto"
    as_of: str | None = None


class ContextRequest(BaseModel):
    query: str
    limit: int = Field(default=6, ge=1, le=30)
    include_recent_raw: bool = False
    conversation_id: str | None = None
    char_budget: int = Field(default=3500, ge=500, le=12000)
    timezone: str | None = None
    as_of: str | None = None
    temporal_scope: Literal["auto", "current", "historical", "future", "all"] = "auto"


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
    event_time: str | None = None
    source_time: str | None = None
    timezone: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None


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
    capture_score: float = Field(default=0.0, ge=0.0, le=1.0)
    repetition_score: float = Field(default=0.0, ge=0.0, le=1.0)
    unfinished_score: float = Field(default=0.0, ge=0.0, le=1.0)
    confirmation_score: float = Field(default=0.0, ge=0.0, le=1.0)
    occurrence_count: int = Field(default=1, ge=1)
    extraction_reasons: list[str] = Field(default_factory=list)
    content_fingerprint: str = ""
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    event_time: str | None = None
    received_at: str | None = None
    persisted_at: str | None = None
    source_time: str | None = None
    timezone: str | None = None
    time_source: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    superseded_by: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    source_channel: str | None = None
    content_origin: Literal["original", "quoted", "summary", "inferred", "generated", "derived"] | None = None
    extractor: str | None = None
    derived_from: list[dict[str, str]] = Field(default_factory=list)
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
    capture_score: float | None = Field(default=None, ge=0.0, le=1.0)
    repetition_score: float | None = Field(default=None, ge=0.0, le=1.0)
    unfinished_score: float | None = Field(default=None, ge=0.0, le=1.0)
    confirmation_score: float | None = Field(default=None, ge=0.0, le=1.0)
    occurrence_count: int | None = Field(default=None, ge=1)
    extraction_reasons: list[str] | None = None
    content_fingerprint: str | None = None
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    event_time: str | None = None
    received_at: str | None = None
    persisted_at: str | None = None
    source_time: str | None = None
    timezone: str | None = None
    time_source: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    superseded_by: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    source_channel: str | None = None
    content_origin: Literal["original", "quoted", "summary", "inferred", "generated", "derived"] | None = None
    extractor: str | None = None
    derived_from: list[dict[str, str]] | None = None
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


class NightlyExtractionRequest(BaseModel):
    conversation_id: str | None = None
    since: str | None = None
    limit: int = Field(default=120, ge=1, le=500)
    model: str | None = None
    dry_run: bool = False


class NightlyCycleRequest(BaseModel):
    since_hours: int = Field(default=36, ge=1, le=24 * 30)
    conversation_id: str | None = None
    limit: int = Field(default=120, ge=1, le=500)
    model: str | None = None
    auto_consolidate: bool = False
    dry_run: bool = False


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
    event_time: str | None = None
    received_at: str | None = None
    persisted_at: str | None = None
    source_time: str | None = None
    timezone: str | None = None
    time_source: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    superseded_by: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    source_channel: str | None = None
    content_origin: Literal["original", "quoted", "summary", "inferred", "generated", "derived"] | None = None
    extractor: str | None = None
    derived_from: list[dict[str, str]] | None = None


class SupersedeMemoryRequest(BaseModel):
    replacement_memory_id: str
    effective_at: str | None = None


class CheckpointRequest(BaseModel):
    reason: str = Field(default="manual", min_length=1, max_length=120)


class BackupCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=40)


class BackupFileRequest(BaseModel):
    filename: str = Field(min_length=4, max_length=255)


class AttributionClaimIn(BaseModel):
    claim_id: str | None = None
    sentence: str | None = None
    claimed_actor_role: Literal["user", "assistant", "system"]
    claim_kind: Literal["speech", "request", "preference", "belief", "proposal"] = "speech"
    statement: str
    event_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)


class AttributionValidateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    conversation_id: str | None = None
    event_ids: list[str] = Field(default_factory=list, max_length=200)
    memory_ids: list[str] = Field(default_factory=list, max_length=100)
    claims: list[AttributionClaimIn] | None = Field(default=None, max_length=100)
    threshold: float = Field(default=0.46, ge=0.20, le=0.95)


class ResponseCandidateIn(BaseModel):
    candidate_id: str | None = None
    content: str = Field(min_length=1, max_length=100000)
    quality_score: float = 0.0
    claims: list[AttributionClaimIn] | None = Field(default=None, max_length=100)


class CandidateSelectRequest(BaseModel):
    candidates: list[ResponseCandidateIn] = Field(min_length=1, max_length=12)
    conversation_id: str | None = None
    event_ids: list[str] = Field(default_factory=list, max_length=200)
    memory_ids: list[str] = Field(default_factory=list, max_length=100)
    threshold: float = Field(default=0.46, ge=0.20, le=0.95)
    validate_temporal: bool = True
    as_of: str | None = None
    timezone: str | None = None


class TemporalValidateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    as_of: str | None = None
    timezone: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, **manager.stats()}


@app.get("/status/phase1")
def phase1_status() -> dict[str, Any]:
    return manager.phase1_status()


@app.get("/status/phase2")
def phase2_status() -> dict[str, Any]:
    return manager.phase2_status()


@app.get("/status/phase3")
def phase3_status() -> dict[str, Any]:
    return manager.phase3_status()


@app.get("/status/phase4")
def phase4_status() -> dict[str, Any]:
    return manager.phase4_status()


@app.get("/status/phase5")
def phase5_status() -> dict[str, Any]:
    return manager.phase5_status()


@app.get("/status/hardening")
def hardening_status() -> dict[str, Any]:
    return manager.hardening_status()


@app.get("/status/nightly")
def nightly_status() -> dict[str, Any]:
    return manager.nightly_status()


@app.get("/status/attribution-gate")
def attribution_gate_status() -> dict[str, Any]:
    return manager.attribution_gate_status()


@app.post("/attribution/validate")
def validate_response_attribution(req: AttributionValidateRequest) -> dict[str, Any]:
    return manager.validate_response_attribution(
        content=req.content,
        conversation_id=req.conversation_id,
        event_ids=req.event_ids,
        memory_ids=req.memory_ids,
        claims=(
            [claim.model_dump(exclude_none=True) for claim in req.claims]
            if req.claims is not None
            else None
        ),
        threshold=req.threshold,
    )


@app.post("/response/candidates/select")
def select_response_candidate(req: CandidateSelectRequest) -> dict[str, Any]:
    return manager.select_response_candidate(
        candidates=[candidate.model_dump(exclude_none=True) for candidate in req.candidates],
        conversation_id=req.conversation_id,
        event_ids=req.event_ids,
        memory_ids=req.memory_ids,
        threshold=req.threshold,
        validate_temporal=req.validate_temporal,
        as_of=req.as_of,
        timezone=req.timezone,
    )


@app.post("/temporal/validate")
def validate_response_temporal(req: TemporalValidateRequest) -> dict[str, Any]:
    try:
        return manager.validate_response_temporal(
            content=req.content,
            as_of=req.as_of,
            timezone=req.timezone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/audit/checkpoints")
def create_signed_checkpoint(req: CheckpointRequest) -> dict[str, Any]:
    try:
        return manager.create_signed_checkpoint(reason=req.reason)
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/audit/checkpoints")
def list_signed_checkpoints(
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    return {"checkpoints": manager.list_checkpoints(limit=limit)}


@app.get("/audit/checkpoints/verify")
def verify_signed_checkpoints() -> dict[str, Any]:
    return manager.verify_checkpoints()


@app.get("/audit/keys")
def list_signing_keys() -> dict[str, Any]:
    return {"keys": manager.list_signing_keys()}


@app.post("/audit/keys/rotate")
def rotate_signing_key() -> dict[str, Any]:
    try:
        return manager.rotate_signing_key()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/audit/branches")
def list_audit_branches() -> dict[str, Any]:
    return {"branches": manager.list_audit_branches()}


@app.post("/backups")
def create_signed_backup(req: BackupCreateRequest) -> dict[str, Any]:
    try:
        return manager.create_signed_backup(label=req.label)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/backups/verify")
def verify_signed_backup(req: BackupFileRequest) -> dict[str, Any]:
    try:
        return manager.verify_signed_backup(req.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/restores/plan")
def plan_backup_restore(req: BackupFileRequest) -> dict[str, Any]:
    try:
        return manager.plan_backup_restore(req.filename)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/audit/verify")
def verify_audit(verify_objects: bool = True) -> dict[str, Any]:
    return manager.verify_audit(verify_objects=verify_objects)


@app.get("/audit/events")
def list_audit_events(
    object_type: Literal["raw_message", "memory_trace", "memory"] | None = None,
    object_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    include_payload: bool = False,
) -> dict[str, Any]:
    return {
        "events": manager.list_audit_events(
            object_type=object_type,
            object_id=object_id,
            limit=limit,
            include_payload=include_payload,
        )
    }


@app.get("/provenance/{object_type}/{object_id}")
def get_provenance(
    object_type: Literal["raw_message", "memory_trace", "memory"],
    object_id: str,
) -> dict[str, Any]:
    return manager.get_provenance(object_type, object_id)


@app.get("/temporal/context")
def temporal_context(
    conversation_id: str | None = None,
    timezone: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    try:
        return manager.build_temporal_context(
            conversation_id=conversation_id,
            timezone=timezone,
            as_of=as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/seed")
def seed(req: SeedRequest) -> dict[str, Any]:
    try:
        return {"inserted": manager.seed(req.path, overwrite=req.overwrite)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/memory/ingest")
def ingest(
    req: IngestRequest,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    try:
        return manager.ingest_messages(
            req.conversation_id,
            [m.model_dump(exclude_none=True) for m in req.messages],
            auto_capture=req.auto_capture,
            capture_threshold=req.capture_threshold,
            default_timezone=req.default_timezone,
            idempotency_key=req.idempotency_key or idempotency_key_header,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
            event_time=req.event_time,
            source_time=req.source_time,
            timezone=req.timezone,
            valid_from=req.valid_from,
            valid_until=req.valid_until,
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
    try:
        results = manager.retrieve(
            req.query,
            limit=req.limit,
            include_archived=req.include_archived,
            memory_types=req.memory_types,
            update_recall=req.update_recall,
            temporal_scope=req.temporal_scope,
            as_of=req.as_of,
        )
        return {"retrieved": [manager.result_to_dict(r) for r in results]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/context/build")
def build_context(req: ContextRequest) -> dict[str, Any]:
    try:
        return manager.build_context(
            req.query,
            limit=req.limit,
            include_recent_raw=req.include_recent_raw,
            conversation_id=req.conversation_id,
            char_budget=req.char_budget,
            timezone=req.timezone,
            as_of=req.as_of,
            temporal_scope=req.temporal_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.delete("/memory/traces/{trace_id}")
def forget_memory_trace(trace_id: str, reason: str = "user_requested") -> dict[str, Any]:
    try:
        return manager.forget_memory_trace(trace_id, reason=reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@app.post("/memory/nightly/extract")
def run_nightly_extraction(req: NightlyExtractionRequest) -> dict[str, Any]:
    try:
        return manager.run_nightly_extraction(
            conversation_id=req.conversation_id,
            since=req.since,
            limit=req.limit,
            model=req.model,
            dry_run=req.dry_run,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/memory/nightly/run")
def run_nightly_cycle(req: NightlyCycleRequest) -> dict[str, Any]:
    try:
        return manager.run_nightly_cycle(
            since_hours=req.since_hours,
            conversation_id=req.conversation_id,
            limit=req.limit,
            model=req.model,
            auto_consolidate=req.auto_consolidate,
            dry_run=req.dry_run,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/memories/retrieve")
def retrieve_long_term_memories(req: RetrieveRequest) -> dict[str, Any]:
    try:
        results = manager.retrieve(
            req.query,
            limit=req.limit,
            include_archived=req.include_archived,
            memory_types=req.memory_types,
            update_recall=req.update_recall,
            temporal_scope=req.temporal_scope,
            as_of=req.as_of,
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
    temporal_scope: Literal["current", "historical", "future", "all"] = "current",
    as_of: str | None = None,
) -> dict[str, Any]:
    return {
        "memories": manager.list_long_term_memories(
            memory_type=memory_type,
            epistemic_status=epistemic_status,
            include_archived=include_archived,
            limit=limit,
            temporal_scope=temporal_scope,
            as_of=as_of,
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


@app.post("/memories/{memory_id}/supersede")
def supersede_long_term_memory(memory_id: str, req: SupersedeMemoryRequest) -> dict[str, Any]:
    try:
        return manager.supersede_long_term_memory(
            memory_id,
            req.replacement_memory_id,
            effective_at=req.effective_at,
        )
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

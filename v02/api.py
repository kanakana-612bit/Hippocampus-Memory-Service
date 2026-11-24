"""Authenticated canonical API; no legacy routes or database initialization on import."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import CONTRACT, VERSION
from .contracts import (AssessmentUpdate, AttemptInput, Contract, EdgeInput, MemoryCreate,
                        MemoryEdit, ObservationInput, ResponseInput, RunInput, SandboxInput,
                        StateInput, SyncInput, UnresolvedInput, WeightUpdate)
from .service import Service
from .storage import Conflict, Missing, SCHEMA_VERSION
from .knowledge_api import register_knowledge_routes
from .knowledge_contracts import POLICY_VERSION as KNOWLEDGE_POLICY
from .extraction_contracts import EXTRACTION_POLICY, EXPLICIT_EXTRACTOR
from .knowledge_observations import OBSERVATION_POLICY
from .knowledge_retrieval_contracts import RETRIEVAL_POLICY
from .control_policy import ControlAssessmentInput, CONTROL_POLICY
from .attention import ATTENTION_SHADOW_POLICY
from .observation import register as register_observation_routes
from .control_catalog import register as register_control_routes
from .episode_api import register_episode_routes
from .episode_contracts import POLICY_VERSION as EPISODE_POLICY


@dataclass(frozen=True)
class Identity:
    principal: str
    host: str
    permissions: frozenset[str]


def load_credentials(path):
    values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(values, list) or not values:
        raise ValueError("A nonempty credential registry is required")
    seen = set()
    for value in values:
        if set(value) != {"token_sha256", "principal_id", "host_id", "permissions"}:
            raise ValueError("Invalid credential registry fields")
        key = value["token_sha256"]
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key) or key in seen:
            raise ValueError("Credential digests must be unique SHA-256 values")
        if not value["principal_id"] or not value["host_id"]:
            raise ValueError("Credential ownership is required")
        if not isinstance(value["permissions"], list) or not value["permissions"] or set(value["permissions"]) - {
                "read", "write", "review", "sync", "observe", "control", "learn", "admin"}:
            raise ValueError("Unknown or empty API permissions")
        seen.add(key)
    return values


class NightlyRequest(Contract):
    conversation_id: str


def create_app(service: Service | None = None, credentials: list[dict] | None = None):
    @asynccontextmanager
    async def lifespan(application):
        if application.state.service is None:
            db = os.getenv("HIPPOCAMPUS_V02_DB")
            auth = os.getenv("HIPPOCAMPUS_V02_AUTH_FILE")
            if not db or not auth:
                raise RuntimeError("Configure HIPPOCAMPUS_V02_DB and HIPPOCAMPUS_V02_AUTH_FILE; legacy settings are not used")
            application.state.credentials = load_credentials(auth)
            application.state.service = Service(db,
                feedback_effects=os.getenv("HIPPOCAMPUS_V02_FEEDBACK_EFFECTS", "0") == "1",
                graph_learning=os.getenv("HIPPOCAMPUS_V02_GRAPH_LEARNING", "0") == "1",
                knowledge_mode=os.getenv("HIPPOCAMPUS_V02_KNOWLEDGE_MODE", "disabled"),
                episode_mode=os.getenv("HIPPOCAMPUS_V02_EPISODE_MODE", "disabled"),
                feedback_ttl=int(os.getenv("HIPPOCAMPUS_V02_FEEDBACK_TTL", "1800")))
        import asyncio
        from .observation import maintain
        maintenance = asyncio.create_task(maintain(application.state.service.db))
        try:
            yield
        finally:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass

    application = FastAPI(title="Hippocampus canonical memory", version=VERSION, lifespan=lifespan)
    application.state.service = service
    application.state.credentials = credentials or []

    def require(permission):
        def authorize(request: Request, authorization: str | None = Header(default=None)):
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(401, "Bearer credential required", headers={"WWW-Authenticate": "Bearer"})
            token_digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
            record = next((r for r in request.app.state.credentials if hmac.compare_digest(token_digest, r["token_sha256"])), None)
            if not record:
                raise HTTPException(401, "Invalid credential")
            identity = Identity(record["principal_id"], record["host_id"], frozenset(record["permissions"]))
            if permission not in identity.permissions:
                raise HTTPException(403, "Permission denied")
            return identity
        return authorize

    def svc(request: Request) -> Service:
        if request.app.state.service is None:
            raise HTTPException(503, "Canonical service is not configured")
        return request.app.state.service

    @application.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(Missing)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={"detail": "Record not found in authorized scope"})

    @application.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @application.exception_handler(PermissionError)
    async def permission_denied(_, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @application.get("/health")
    def health():
        return {"status": "ok", "contract": CONTRACT}

    prefix = "/api/v0.2"
    register_knowledge_routes(application, prefix, require, svc)
    register_observation_routes(application, prefix, require, svc)
    register_control_routes(application, prefix, require, svc)
    register_episode_routes(application, prefix, require, svc)

    @application.get(prefix + "/status")
    def status(identity=Depends(require("read")), service=Depends(svc)):
        return {"version": VERSION, "contract": CONTRACT, "legacy_api": False, "context_cap": None,
                "query_updates_weights": False, "generalization_learning": False,
                "feedback_effects": service.feedback_effects, "association_learning": service.graph_learning,
                "realtime_model_calls": False, "provider_internal_observation": False,
                "packet_contract":"hippocampus.packet.v0.4" if service.episode_mode == "enabled" else "hippocampus.packet.v0.3", "dispatch_validation_required":True,
                "episodes":{"policy":EPISODE_POLICY,"schema_version":SCHEMA_VERSION,"mode":service.episode_mode,
                    "raw_preserved":True,"automatic_adoption":False,"worker_autostart":False,
                    "daily_timezone":"Asia/Tokyo","daily_hour":3,"inactivity_seconds":1800},
                "control_assessment_policy":CONTROL_POLICY, "attention_shadow_policy":ATTENTION_SHADOW_POLICY,
                "knowledge": {"stage": "K4", "contract": KNOWLEDGE_POLICY, "schema_version": SCHEMA_VERSION,
                    "extraction_contract": EXTRACTION_POLICY, "extractor_version": EXPLICIT_EXTRACTOR,
                    "explicit_proposal_acceptance": True,
                    "manual_review": True, "automatic_extraction": False,
                    "countersearch_execution": True, "countersearch_backend": "internal_canonical_sources",
                    "external_countersearch": False, "automatic_review": False,
                    "observation_contract": OBSERVATION_POLICY, "retrieval_contract":RETRIEVAL_POLICY,
                    "mode":service.knowledge_mode,"context_injection": service.knowledge_mode == "enabled"}}

    @application.post(prefix + "/control/assessments/{state_id}")
    def control_assessment(state_id: str, req: ControlAssessmentInput, identity=Depends(require("control")), service=Depends(svc)):
        return service.assess_control(identity.principal, identity.host, state_id, req.model_dump())

    @application.post(prefix + "/dialogue/sync")
    def sync(req: SyncInput, identity=Depends(require("sync")), service=Depends(svc)):
        return service.sync(identity.principal, req.model_dump())

    @application.get(prefix + "/dialogue/path")
    def dialogue_path(conversation_id: str, identity=Depends(require("read")), service=Depends(svc)):
        with service.db.connection() as con:
            return service.path(con, identity.principal, conversation_id)

    @application.post(prefix + "/memories")
    def create(req: MemoryCreate, identity=Depends(require("write")), service=Depends(svc)):
        return service.create_memory(identity.principal, req.model_dump())

    @application.patch(prefix + "/memories/{memory_id}")
    def edit(memory_id: str, req: MemoryEdit, identity=Depends(require("write")), service=Depends(svc)):
        return service.update_memory(identity.principal, memory_id, req.model_dump(exclude_unset=True), "content")

    @application.patch(prefix + "/memories/{memory_id}/weights")
    def weights(memory_id: str, req: WeightUpdate, identity=Depends(require("review")), service=Depends(svc)):
        return service.update_memory(identity.principal, memory_id, req.model_dump(exclude_unset=True), "weights")

    @application.patch(prefix + "/memories/{memory_id}/assessment")
    def assess(memory_id: str, req: AssessmentUpdate, identity=Depends(require("review")), service=Depends(svc)):
        return service.update_memory(identity.principal, memory_id, req.model_dump(), "assessment")

    @application.get(prefix + "/records/{record_id}")
    def record(record_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.get(identity.principal, record_id)

    @application.get(prefix + "/records")
    def records(kind: Literal["memory", "event", "feedback", "modifier", "state", "sandbox", "edge", "unresolved", "run", "attempt", "path", "observation",
                             "knowledge_claim", "knowledge_claim_version", "knowledge_source", "knowledge_evidence", "knowledge_search", "knowledge_review",
                             "knowledge_extraction", "knowledge_migration", "knowledge_search_plan", "knowledge_search_attempt",
                             "knowledge_search_observation", "knowledge_search_lease"],
                conversation_id: str | None = None, identity=Depends(require("read")), service=Depends(svc)):
        return service.list_records(identity.principal, kind, conversation_id)

    @application.post(prefix + "/context/runs")
    def run(req: RunInput, identity=Depends(require("read")), service=Depends(svc)):
        return service.build_run(identity.principal, req.model_dump())

    @application.post(prefix + "/attempts")
    def attempt(req: AttemptInput, identity=Depends(require("observe")), service=Depends(svc)):
        return service.attempt(identity.principal, identity.host, req.model_dump())

    @application.post(prefix + "/attempts/{attempt_id}/observations")
    def observe(attempt_id: str, req: ObservationInput, identity=Depends(require("observe")), service=Depends(svc)):
        return service.observe(identity.principal, identity.host, attempt_id, req.model_dump())

    @application.post(prefix + "/attempts/{attempt_id}/complete")
    def complete(attempt_id: str, req: ResponseInput, identity=Depends(require("observe")), service=Depends(svc)):
        return service.complete(identity.principal, identity.host, attempt_id, req.model_dump())

    @application.put(prefix + "/states/{state_id}")
    def state(state_id: str, req: StateInput, identity=Depends(require("control")), service=Depends(svc)):
        return service.functional_state(identity.principal, state_id, req.model_dump())

    @application.post(prefix + "/sandbox")
    def sandbox(req: SandboxInput, identity=Depends(require("control")), service=Depends(svc)):
        return service.sandbox(identity.principal, req.model_dump())

    @application.put(prefix + "/unresolved/{item_id}")
    def unresolved(item_id: str, req: UnresolvedInput, identity=Depends(require("control")), service=Depends(svc)):
        return service.unresolved(identity.principal, item_id, req.model_dump())

    @application.post(prefix + "/edges")
    def edge(req: EdgeInput, identity=Depends(require("learn")), service=Depends(svc)):
        return service.learn_edge(identity.principal, req.model_dump())

    @application.post(prefix + "/edges/preview")
    def edge_preview(req: EdgeInput, identity=Depends(require("learn")), service=Depends(svc)):
        return service.learn_edge(identity.principal, req.model_dump(), shadow=True)

    @application.post(prefix + "/nightly/run")
    def nightly(req: NightlyRequest, identity=Depends(require("write")), service=Depends(svc)):
        return service.nightly(identity.principal, req.conversation_id)

    @application.get(prefix + "/audit/verify")
    def verify(identity=Depends(require("admin")), service=Depends(svc)):
        return service.db.verify()

    @application.post(prefix + "/backups")
    def backup(identity=Depends(require("admin")), service=Depends(svc)):
        return service.db.backup()

    return application


app = create_app()

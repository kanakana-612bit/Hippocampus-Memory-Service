"""An independent v1 API. No old routes, old credentials or automatic deployment."""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from . import VERSION
from .access import effective_identity, enforce, require
from .analysis import ProviderAnalyzer
from .common import Conflict, Denied, Invalid
from .contracts import Condition, Contract, EthicalWeights, Identity, SourceRange, SourceRegister, Text, TimeInterpretation
from .learning import Learning
from .memory import Memory


class SourceRequest(Contract):
    registration: SourceRegister
    raw_json: str
    key: Text


class RunRequest(Contract):
    mode: Literal["initial", "daily", "reanalysis"]
    ranges: tuple[SourceRange, ...] = ()
    source_ids: tuple[str, ...] = ()
    model_id: str | None = None
    key: Text


class KeyRequest(Contract):
    key: Text


class ReviewRequest(KeyRequest):
    reason: Text
    expected_version: int = Field(ge=1, strict=True)


class TimeRequest(ReviewRequest):
    interpretation: TimeInterpretation
    allow_downgrade: bool = False


class SituationRequest(KeyRequest):
    room_name: str | None = None
    audience: Literal["owner", "guest", "public"]
    expires_at: Text


class ModifierRequest(KeyRequest):
    context_id: Text
    condition: Condition
    modifier: float = Field(ge=-1, le=1, strict=True)
    expires_at: Text
    reason: Text


class EthicsRequest(KeyRequest):
    record_id: Text
    weights: EthicalWeights
    reason: Text


class PolicyRequest(ReviewRequest):
    target_kind: Literal["source", "record"]
    target_id: Text
    scope: Literal["public", "personal", "private", "restricted"]
    destinations: tuple[Text, ...]
    allow_expansion: bool = False


class RetrievalRequest(KeyRequest):
    query: str
    limit: int = Field(default=10, ge=1, le=100, strict=True)
    context_window: int = Field(default=32768, ge=512, strict=True)
    reserved_tokens: int = Field(default=8192, ge=0, strict=True)
    recent_raw_budget: int = Field(default=12500, ge=0, strict=True)
    source_id: str | None = None


class FinalizeRequest(KeyRequest):
    attempt: int = Field(ge=1, strict=True)
    path_revision: Literal[1] = 1


class MembershipRequest(KeyRequest):
    record_id: Text
    context_id: Text | None = None
    name: Text | None = None
    description: Text | None = None
    weight: float = Field(ge=0, le=1, strict=True)
    confidence: float | None = Field(default=None, ge=0, le=1, strict=True)
    expected_map_version: int = Field(ge=0, strict=True)
    expected_record_version: int = Field(ge=1, strict=True)
    reason: Text


def create_app(storage, *, models, default_model, credentials, analyzer=None):
    if not credentials or any(len(token) < 24 for token in credentials):
        raise Invalid("Independent v1 bearer credentials of at least 24 characters are required")
    credentials = {token: Identity.model_validate(identity) for token, identity in credentials.items()}
    learning = Learning(storage, models, default_model, analyzer or ProviderAnalyzer())
    memory = Memory(storage)
    app = FastAPI(title="Hippocampus Ver1.0 synthetic development", version=VERSION, docs_url=None, redoc_url=None)
    app.state.learning, app.state.memory = learning, memory

    def authenticate(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Independent v1 credential required")
        supplied = authorization[7:]
        for token, identity in credentials.items():
            if secrets.compare_digest(supplied, token):
                return identity
        raise HTTPException(401, "Invalid credential")

    @app.middleware("http")
    async def no_sensitive_cache(request: Request, call_next):
        # Never allow browser caching of original text, staging, selections or receipts.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(Denied)
    async def denied(request, exc):
        return JSONResponse(status_code=403, content={"error": "Object or operation unavailable in this authorization context"})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(Invalid)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default 'input' echo could expose submitted original text or tokens.
        return JSONResponse(status_code=422, content={"error": "Request does not satisfy the v1 contract",
            "fields": [list(item["loc"]) for item in exc.errors()]})

    @app.get("/")
    def ui():
        return FileResponse(Path(__file__).with_name("ui") / "index.html")

    @app.get("/ui/{name}")
    def assets(name: str):
        if name not in ("app.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).with_name("ui") / name)

    @app.get("/api/v1/meta")
    def meta(identity=Depends(authenticate)):
        return {"version": VERSION, "schema": storage.marker["schema"], "data_class": "synthetic",
            "principal_id": identity.principal_id, "permissions": sorted(identity.permissions),
            "real_intake_enabled": False, "legacy_routes": False}

    @app.get("/api/v1/models")
    def models_list(identity=Depends(authenticate)):
        require(identity, "learning.run")
        return {"default": default_model, "models": [m.model_dump(exclude={"endpoint"}) for m in models]}

    @app.post("/api/v1/learning/sources")
    def register(body: SourceRequest, identity=Depends(authenticate)):
        return learning.register_source(identity, body.registration, body.raw_json.encode("utf-8"), key=body.key)

    @app.get("/api/v1/learning/sources")
    def sources(identity=Depends(authenticate)):
        require(identity, "learning.run")
        with storage.connection() as con:
            effective, _ = effective_identity(con, identity)
            result = []
            for source in con.execute("SELECT * FROM raw_sources WHERE principal_id=?", (identity.principal_id,)):
                try:
                    enforce(effective, source)
                except Denied:
                    continue
                snapshot = con.execute("SELECT message_count,digest FROM raw_snapshots WHERE id=?", (source["head_snapshot_id"],)).fetchone()
                checkpoint = con.execute("SELECT sequence FROM learning_checkpoints WHERE source_id=?", (source["id"],)).fetchone()
                result.append({"id": source["id"], "name": source["name"], "version": source["version"],
                    "message_count": snapshot["message_count"], "digest": snapshot["digest"], "checkpoint": checkpoint[0],
                    "access_scope": source["access_scope"], "timezone": source["timezone"]})
            return {"sources": result}

    @app.post("/api/v1/learning/runs")
    def create_run(body: RunRequest, identity=Depends(authenticate)):
        return learning.create_run(identity, **body.model_dump())

    def run_detail(identity, run_id):
        require(identity, "learning.run")
        with storage.connection() as con:
            effective, _ = effective_identity(con, identity)
            run = con.execute("SELECT * FROM learning_runs WHERE id=? AND principal_id=?", (run_id, identity.principal_id)).fetchone()
            if not run:
                raise Denied()
            manifest = json.loads(run["manifest_json"])
            for source in manifest["sources"]:
                enforce(effective, con.execute("SELECT * FROM raw_sources WHERE id=?", (source["source_id"],)).fetchone())
            batches = [dict(r) for r in con.execute("SELECT id,source_id,first_sequence,last_sequence,status,version FROM learning_batches WHERE run_id=?", (run_id,))]
            return {"id": run_id, "mode": run["mode"], "status": run["status"], "release_status": run["release_status"],
                    "manifest": manifest, "batches": batches, "version": run["version"]}

    @app.get("/api/v1/learning/runs")
    def runs(identity=Depends(authenticate)):
        require(identity, "learning.run")
        with storage.connection() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM learning_runs WHERE principal_id=?", (identity.principal_id,))]
        result = []
        for run_id in ids:
            try:
                result.append(run_detail(identity, run_id))
            except Denied:
                pass
        return {"runs": result}

    @app.get("/api/v1/learning/runs/{run_id}")
    def run(run_id: str, identity=Depends(authenticate)):
        return run_detail(identity, run_id)

    @app.get("/api/v1/learning/staging/{batch_id}")
    def staging(batch_id: str, identity=Depends(authenticate)):
        require(identity, "learning.run")
        with storage.connection() as con:
            batch, _, _ = learning._batch(con, identity, batch_id)
            row = con.execute("SELECT * FROM staging WHERE batch_id=?", (batch_id,)).fetchone()
            return {"batch_id": batch_id, "input": json.loads(batch["input_json"]), "status": row["status"] if row else "pending",
                    "output": json.loads(row["output_json"]) if row and row["output_json"] else None,
                    "validation": json.loads(row["validation_json"]) if row else None, "version": row["version"] if row else None}

    @app.post("/api/v1/learning/batches/{batch_id}/analyze")
    def analyze(batch_id: str, body: KeyRequest, identity=Depends(authenticate)):
        return learning.analyze(identity, batch_id, key=body.key)

    @app.post("/api/v1/learning/batches/{batch_id}/commit")
    def commit(batch_id: str, body: KeyRequest, identity=Depends(authenticate)):
        return learning.commit(identity, batch_id, key=body.key)

    @app.post("/api/v1/learning/batches/{batch_id}/review")
    def review(batch_id: str, body: ReviewRequest, identity=Depends(authenticate)):
        return learning.review(identity, batch_id, **body.model_dump())

    @app.get("/api/v1/dialogue/timeline")
    @app.get("/api/v1/dialogue/search")
    @app.get("/api/v1/dialogue/exports")
    def history(query: str = "", kind: str | None = None, day: str | None = None, timezone_name: str = "Asia/Tokyo",
                time_bucket: str | None = None, limit: int = Query(100, ge=1, le=500), cursor: str | None = None, identity=Depends(authenticate)):
        return memory.timeline(identity, query=query, kind=kind, day=day, timezone_name=timezone_name, time_bucket=time_bucket, limit=limit, cursor=cursor)

    @app.get("/api/v1/records/{record_id}")
    def record(record_id: str, identity=Depends(authenticate)):
        return memory.get(identity, record_id)

    @app.get("/api/v1/dialogue/recent")
    def recent(source_id: str, identity=Depends(authenticate)):
        return memory.recent_turns(identity, source_id)

    @app.post("/api/v1/dialogue/events/{event_id}/time-revisions")
    def time_revision(event_id: str, body: TimeRequest, identity=Depends(authenticate)):
        return memory.correct_time(identity, event_id, body.interpretation, **body.model_dump(exclude={"interpretation"}))

    @app.get("/api/v1/dialogue/events/{event_id}/time-revisions")
    def time_history(event_id: str, identity=Depends(authenticate)):
        return {"revisions": memory.time_history(identity, event_id)}

    @app.get("/api/v1/contexts")
    @app.get("/api/v1/context-maps")
    def contexts(identity=Depends(authenticate)):
        return memory.contexts(identity)

    @app.post("/api/v1/context-maps/assign")
    def membership(body: MembershipRequest, identity=Depends(authenticate)):
        return memory.assign_context(identity, **body.model_dump())

    @app.post("/api/v1/situations")
    def situation(body: SituationRequest, identity=Depends(authenticate)):
        return memory.set_situation(identity, **body.model_dump())

    @app.get("/api/v1/situations/current")
    def current_situation(identity=Depends(authenticate)):
        require(identity, "records.read")
        with storage.connection() as con:
            effective, situation = effective_identity(con, identity)
            room = con.execute("SELECT name FROM rooms WHERE id=? AND principal_id=?",
                (situation["room_id"], identity.principal_id)).fetchone() if situation and situation["room_id"] else None
            return {"audience": effective.audience, "surface_id": effective.surface_id,
                "room_name": room[0] if room else None, "situation_id": situation["id"] if situation else None,
                "expires_at": situation["expires_at"] if situation else None}

    @app.post("/api/v1/context-policies/modifiers")
    def modifier(body: ModifierRequest, identity=Depends(authenticate)):
        return memory.set_modifier(identity, body.context_id, body.condition, **body.model_dump(exclude={"condition", "context_id"}))

    @app.post("/api/v1/context-policies/ethics")
    def ethics(body: EthicsRequest, identity=Depends(authenticate)):
        return memory.set_ethics(identity, body.record_id, body.weights, **body.model_dump(exclude={"record_id", "weights"}))

    @app.post("/api/v1/access-policies")
    def policy(body: PolicyRequest, identity=Depends(authenticate)):
        return memory.set_policy(identity, **body.model_dump())

    @app.post("/api/v1/context/runs")
    def recall(body: RetrievalRequest, identity=Depends(authenticate)):
        return memory.prepare(identity, **body.model_dump())

    @app.post("/api/v1/context/runs/{run_id}/finalize")
    def finalize(run_id: str, body: FinalizeRequest, identity=Depends(authenticate)):
        return memory.finalize(identity, run_id, **body.model_dump())

    return app

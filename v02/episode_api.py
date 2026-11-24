"""Canonical episodic maintenance and recall; no model call in API transactions."""
from fastapi import Depends
from .episode_contracts import ArtifactReferenceCreate, ChunkSeal, EpisodeQuery, JobCommit, JobPrepare, StructuredReview


def register_episode_routes(application, prefix, require, svc):
    root = prefix + "/episodes"

    @application.post(root + "/chunks/seal")
    def seal(req: ChunkSeal, identity=Depends(require("write")), service=Depends(svc)):
        return service.episodes.seal(identity.principal, req.model_dump())

    @application.post(root + "/jobs/prepare")
    def prepare(req: JobPrepare, identity=Depends(require("write")), service=Depends(svc)):
        return service.episodes.prepare(identity.principal, identity.host, req.model_dump())

    @application.post(root + "/jobs/{job_id}/commit")
    def commit(job_id: str, req: JobCommit, identity=Depends(require("write")), service=Depends(svc)):
        return service.episodes.commit(identity.principal, identity.host, job_id, req.model_dump())

    @application.post(root + "/query")
    def query(req: EpisodeQuery, identity=Depends(require("read")), service=Depends(svc)):
        return service.episodes.query(identity.principal, req.model_dump())

    @application.post(root + "/structured/reviews")
    def review(req: StructuredReview, identity=Depends(require("review")), service=Depends(svc)):
        return service.episodes.review(identity.principal, identity.host, req.model_dump())

    @application.post(root + "/artifacts")
    def artifact(req: ArtifactReferenceCreate, identity=Depends(require("review")), service=Depends(svc)):
        return service.episodes.artifact(identity.principal, identity.host, req.model_dump())

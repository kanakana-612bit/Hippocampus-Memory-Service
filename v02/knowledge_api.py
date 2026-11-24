"""Authenticated knowledge routes; inspection is separate from model-facing use."""
from fastapi import Depends

from .knowledge_commands import (ClaimCreate, ClaimEdit, ClaimReview, EvidenceReplace,
                                  SearchRecord, SourceCreate)
from .extraction_contracts import ExtractionSubmit
from .countersearch_contracts import SearchPlanCreate, SearchExecute
from .knowledge_observations import ObservationCreate


def register_knowledge_routes(application, prefix, require, svc):
    root = prefix + "/knowledge"

    @application.post(root + "/claims/{claim_id}/search-plans")
    def plan(claim_id: str, req: SearchPlanCreate, identity=Depends(require("review")), service=Depends(svc)):
        return service.countersearch.create_plan(identity, claim_id, req.model_dump())

    @application.post(root + "/search-plans/{plan_id}/execute")
    def execute(plan_id: str, req: SearchExecute, identity=Depends(require("review")), service=Depends(svc)):
        return service.countersearch.execute(identity, plan_id, req.model_dump())

    @application.get(root + "/search-plans/{plan_id}")
    def get_plan(plan_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.countersearch.get_plan(identity, plan_id)

    @application.get(root + "/search-attempts/{attempt_id}")
    def get_attempt(attempt_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.countersearch.get_attempt(identity, attempt_id)

    @application.post(root + "/claims/{claim_id}/search-observations")
    def observe(claim_id: str, req: ObservationCreate, identity=Depends(require("review")), service=Depends(svc)):
        return service.knowledge_observations.record(identity, claim_id, req.model_dump())

    @application.get(root + "/search-observations/{observation_id}")
    def get_observation(observation_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.knowledge_observations.get(identity, observation_id)

    @application.post(root + "/extractions")
    def extract(req: ExtractionSubmit, identity=Depends(require("write")), service=Depends(svc)):
        return service.extraction.submit(identity, req.model_dump())

    @application.get(root + "/extractions/{extraction_id}")
    def extraction(extraction_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.extraction.get(identity, extraction_id)

    @application.post(root + "/sources")
    def source(req: SourceCreate, identity=Depends(require("write")), service=Depends(svc)):
        return service.knowledge.create_source(identity, req.model_dump())

    @application.post(root + "/claims")
    def create(req: ClaimCreate, identity=Depends(require("write")), service=Depends(svc)):
        return service.knowledge.create_claim(identity, req.model_dump())

    @application.get(root + "/claims")
    def claims(conversation_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.list_records(identity.principal, "knowledge_claim", conversation_id)

    @application.get(root + "/claims/{claim_id}")
    def claim(claim_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.knowledge.get_claim(identity, claim_id)

    @application.patch(root + "/claims/{claim_id}")
    def edit(claim_id: str, req: ClaimEdit, identity=Depends(require("write")), service=Depends(svc)):
        return service.knowledge.edit_claim(identity, claim_id, req.model_dump())

    @application.put(root + "/claims/{claim_id}/evidence")
    def evidence(claim_id: str, req: EvidenceReplace, identity=Depends(require("review")), service=Depends(svc)):
        return service.knowledge.replace_evidence(identity, claim_id, req.model_dump())

    @application.post(root + "/claims/{claim_id}/searches")
    def search(claim_id: str, req: SearchRecord, identity=Depends(require("review")), service=Depends(svc)):
        return service.knowledge.record_search(identity, claim_id, req.model_dump())

    @application.post(root + "/claims/{claim_id}/reviews")
    def review(claim_id: str, req: ClaimReview, identity=Depends(require("review")), service=Depends(svc)):
        return service.knowledge.review_claim(identity, claim_id, req.model_dump())

    @application.get(root + "/claims/{claim_id}/history")
    def history(claim_id: str, identity=Depends(require("read")), service=Depends(svc)):
        return service.knowledge.history(identity, claim_id)

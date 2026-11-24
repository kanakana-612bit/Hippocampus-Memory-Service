"""Read-only discovery and validation of existing canonical commands.

No UI, credentials, rendering, or alternate write path belongs in this module.
Execution still uses the existing authenticated domain endpoints.
"""
from fastapi import Depends, HTTPException
from pydantic import Field

from .contracts import Contract, WeightUpdate, StateInput, SandboxInput, UnresolvedInput, RunInput, uid
from .countersearch_contracts import SearchPlanCreate, SearchExecute
from .knowledge_commands import ClaimReview, EvidenceReplace
from .knowledge_observations import ObservationCreate
from .knowledge_contracts import POLICY_VERSION
from .storage import Missing, Conflict

OPERATIONS = {
    "memory.weights": (WeightUpdate, "review", "PATCH", "/memories/{target}/weights", "memory", "記憶の重み"),
    "state.set": (StateInput, "control", "PUT", "/states/{target}", "state", "機能状態"),
    "sandbox.set": (SandboxInput, "control", "POST", "/sandbox", "sandbox", "表現サンドボックス"),
    "unresolved.set": (UnresolvedInput, "control", "PUT", "/unresolved/{target}", "unresolved", "未解決項目"),
    "retrieval.run": (RunInput, "read", "POST", "/context/runs", None, "内部検索・関連探索"),
    "search.plan": (SearchPlanCreate, "review", "POST", "/knowledge/claims/{target}/search-plans", "knowledge_claim", "内部反証探索の計画"),
    "search.execute": (SearchExecute, "review", "POST", "/knowledge/search-plans/{target}/execute", "knowledge_search_plan", "内部反証探索の実行"),
    "claim.evidence": (EvidenceReplace, "review", "PUT", "/knowledge/claims/{target}/evidence", "knowledge_claim", "主張の根拠更新"),
    "claim.inspect": (ObservationCreate, "review", "POST", "/knowledge/claims/{target}/search-observations", "knowledge_claim", "出典・反証の照合"),
    "claim.review": (ClaimReview, "review", "POST", "/knowledge/claims/{target}/reviews", "knowledge_claim", "認識論的審査"),
}
STATE_DIMENSIONS = ("uncertainty_load", "attention_urgency", "exploration_readiness", "caution_gain", "integrity_pressure", "information_gap")
RECEIPTS = {"memory.weights": "memory.weights", "state.set": "state", "sandbox.set": "sandbox", "unresolved.set": "unresolved",
    "retrieval.run": "context.build", "search.plan": "knowledge.countersearch.plan", "search.execute": "knowledge.countersearch.finish",
    "claim.evidence": "knowledge.evidence.replace", "claim.inspect": "knowledge.search.observe", "claim.review": "knowledge.claim.review"}


class Validation(Contract):
    operation: str
    target_id: str | None = None
    conversation_id: str = Field(min_length=1)
    sandbox_id: str | None = None
    path_revision: str
    payload: dict


def validate(service, identity, req):
    if req.operation not in OPERATIONS:
        raise HTTPException(422, "Unsupported operation")
    model, permission, method, route, kind, label = OPERATIONS[req.operation]
    if permission not in identity.permissions:
        raise HTTPException(403, "Permission denied")
    parsed = model.model_validate(req.payload)
    payload = parsed.model_dump(mode="json", exclude_unset=True)
    if req.operation == "memory.weights" and not any(k in payload for k in ("retrieval_weight", "salience", "stability")):
        raise ValueError("At least one weight is required")
    if req.operation == "state.set" and (not parsed.values or set(parsed.values) - set(STATE_DIMENSIONS)):
        raise ValueError("Unsupported or empty functional state")
    if req.operation == "unresolved.set" and req.sandbox_id is not None:
        raise ValueError("Unresolved items currently support conversation scope only")
    supplied_scope = payload.get("scope", {})
    if payload.get("conversation_id", supplied_scope.get("conversation_id", req.conversation_id)) != req.conversation_id:
        raise ValueError("Conversation scope mismatch")
    if payload.get("sandbox_id", supplied_scope.get("sandbox_id", req.sandbox_id)) != req.sandbox_id:
        raise ValueError("Sandbox scope mismatch")
    if payload.get("path_revision", req.path_revision) != req.path_revision:
        raise Conflict("Path revision mismatch")
    target = req.target_id
    current = None
    with service.db.connection() as con:
        path = service.path(con, identity.principal, req.conversation_id)
        if path["data"]["revision_key"] != req.path_revision:
            raise Conflict("Selected path changed; reload current values")
        if req.operation == "sandbox.set":
            target = uid("sandbox", identity.principal, req.conversation_id, parsed.sandbox_id)
        if kind:
            if not target or len(target) > 512:
                raise ValueError("A target ID is required")
            try:
                current = service.db.get(con, identity.principal, target, kind)
            except Missing:
                if req.operation not in {"state.set", "sandbox.set", "unresolved.set"}:
                    raise
            if current:
                d = current["data"].get("snapshot", current["data"])
                scope = d.get("scope", {})
                if current["conversation_id"] != req.conversation_id or d.get("sandbox_id", scope.get("sandbox_id")) != req.sandbox_id:
                    raise HTTPException(404, "Target not found in selected scope")
                if kind == "memory" and not service.eligible(con, identity.principal, current["data"], path):
                    raise Conflict("Target is outside the selected path")
                if "expected_version" in model.model_fields and parsed.expected_version != current["version"]:
                    raise Conflict("Stale record version")
                for field in ("claim_revision", "evidence_set_revision"):
                    if field in payload and payload[field] != d.get(field):
                        raise Conflict("Stale claim/evidence revision")
            elif payload.get("expected_version") is not None:
                raise Conflict("Target no longer exists")
    before = current["data"].get("snapshot", current["data"]) if current else {}
    excluded = {"idempotency_key", "reason", "expected_version", "path_revision", "claim_revision", "evidence_set_revision"}
    return {"valid": True, "operation": req.operation, "label": label, "target_id": target,
            "method": method, "route": route, "payload": payload, "current_version": current["version"] if current else None,
            "changes": [{"field": k, "before": before.get(k), "after": v} for k, v in payload.items() if k not in excluded and before.get(k) != v],
            "checks": ["structure", "permission", "scope", "expected_version"],
            "notice": "適用時に本体が再検証します。採用条件・出典整合性の最終判定は実行時です。"}


def register(application, prefix, require, svc):
    @application.get(prefix + "/control/receipts/{key}")
    def receipt(key: str, operation: str, identity=Depends(require("read")), service=Depends(svc)):
        if operation not in OPERATIONS or OPERATIONS[operation][1] not in identity.permissions:
            raise HTTPException(403, "Permission denied")
        with service.db.connection() as con:
            record = service.db.get(con, identity.principal, uid("receipt", identity.principal, RECEIPTS[operation], key), "receipt")
            return {"receipt_ref": {"id": record["id"], "version": record["version"]}, "result": record["data"]["result"]}

    @application.get(prefix + "/control/catalog")
    def catalog(identity=Depends(require("read"))):
        return {"contract": "hippocampus.control-catalog.v1", "policy_version": POLICY_VERSION,
            "state_dimensions": STATE_DIMENSIONS,
            "operations": [{"id": key, "label": entry[5], "permission": entry[1], "allowed": entry[1] in identity.permissions,
                "target_kind": entry[4], "schema": entry[0].model_json_schema(), "owner": "hippocampus", "applies": "command_commit"}
                for key, entry in OPERATIONS.items()],
            "unsupported": {"autonomous_budget": "scheduler未接続", "web_search_depth": "Web connector未接続",
                "persistent_graph_depth": "検索ごとのgraph_hopsのみ対応", "exploration_priority": "優先度queue未接続",
                "cancel_running_search": "同期実行の途中停止は未対応", "generalization_weight": "学習契約未実装",
                "immunity_policy_edit": "固定policy。profileと根拠は審査ごとに設定"}}

    @application.post(prefix + "/control/validate")
    def validate_command(req: Validation, identity=Depends(require("read")), service=Depends(svc)):
        return validate(service, identity, req)

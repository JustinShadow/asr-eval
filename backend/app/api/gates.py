"""门禁策略管理与 Evaluation 门禁判定接口（M7 §12）。"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.api.idempotency import apply_idempotent_response
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain import audit
from app.domain.etag import check_if_match, require_if_match
from app.domain.gates import (
    create_gate_policy,
    evaluate_gate,
    gate_policy_etag,
    serialize_gate_policy,
    validate_rules,
)
from app.domain.idempotency import execute_idempotently
from app.domain.models import Benchmark, GatePolicy

router = APIRouter(prefix="/api/v1", tags=["gates"])


class GatePolicyCreate(BaseModel):
    """新建门禁策略；默认关闭，避免未审核阈值影响现有评测。"""

    name: str = Field(min_length=1, max_length=128)
    benchmark_id: uuid.UUID | None = None
    enabled: bool = False
    rules: list[dict[str, Any]] = Field(default_factory=list)
    on_missing: Literal["skip", "fail"] = "skip"


class GatePolicyPatch(BaseModel):
    """局部更新门禁策略配置。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    benchmark_id: uuid.UUID | None = None
    enabled: bool | None = None
    rules: list[dict[str, Any]] | None = None
    on_missing: Literal["skip", "fail"] | None = None

    @model_validator(mode="after")
    def check_fields(self) -> "GatePolicyPatch":
        """拒绝空更新，并禁止对必填配置字段显式传 null。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        for field in self.model_fields_set - {"benchmark_id"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        return self


@router.get("/gate-policies")
def list_gate_policies(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("admin:system")),
) -> dict[str, Any]:
    """列出门禁策略；每项携带 ETag，供后续 PATCH 防止覆盖并发修改。"""
    policies = db.scalars(
        select(GatePolicy).order_by(GatePolicy.updated_at.desc(), GatePolicy.id)
    ).all()
    return {"items": [serialize_gate_policy(policy) for policy in policies]}


@router.post("/gate-policies", status_code=201)
def post_gate_policy(
    body: GatePolicyCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("admin:system")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建策略并在同一事务记录审计；重试由 Idempotency-Key 去重。"""

    def create() -> dict[str, Any]:
        policy = create_gate_policy(
            db,
            name=body.name,
            benchmark_id=body.benchmark_id,
            enabled=body.enabled,
            rules=body.rules,
            on_missing=body.on_missing,
            actor=str(claims.get("sub", "")),
        )
        return serialize_gate_policy(policy)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=create,
        location=lambda value: f"/api/v1/gate-policies/{value['id']}",
    )
    apply_idempotent_response(response, result)
    policy_etag_value = result.body.get("etag")
    if isinstance(policy_etag_value, str):
        response.headers["ETag"] = policy_etag_value
    return result.body


@router.patch("/gate-policies/{policy_id}")
def patch_gate_policy(
    policy_id: uuid.UUID,
    body: GatePolicyPatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("admin:system")),
) -> dict[str, Any]:
    """强制 If-Match 更新；并发冲突回传当前配置并与审计一起提交。"""
    supplied_etag = require_if_match(if_match)
    policy = db.scalar(
        select(GatePolicy)
        .where(GatePolicy.id == policy_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if policy is None:
        raise ApiError("E_NOT_FOUND", "门禁策略不存在", status_code=404)

    attempted = body.model_dump(exclude_unset=True, mode="json")
    current = serialize_gate_policy(policy)
    check_if_match(
        supplied_etag,
        gate_policy_etag(policy),
        current=current,
        attempted=attempted,
    )
    changes = body.model_dump(exclude_unset=True)
    benchmark_id = changes.get("benchmark_id", policy.benchmark_id)
    if benchmark_id is not None and db.get(Benchmark, benchmark_id) is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    new_rules = validate_rules(changes["rules"]) if "rules" in changes else list(policy.rules or [])
    new_enabled = changes.get("enabled", policy.enabled)
    if new_enabled and not new_rules:
        raise ApiError(
            "E_GATE_POLICY_INVALID",
            "启用门禁策略至少需要一条规则",
            status_code=422,
            detail={"errors": [{"path": "rules", "reason": "empty_enabled_policy"}]},
        )

    previous_etag = gate_policy_etag(policy)
    for field in ("name", "benchmark_id", "enabled", "on_missing"):
        if field in changes:
            setattr(policy, field, changes[field])
    if "rules" in changes:
        policy.rules = new_rules
    actor = str(claims.get("sub", ""))
    policy.updated_by = actor
    policy.version += 1
    db.flush()
    audit.record(
        db,
        "gate_policy.updated",
        actor=actor,
        resource=str(policy.id),
        detail={
            "changes": attempted,
            "force": False,
            "previous_etag": previous_etag,
            "version": policy.version,
        },
    )
    db.commit()
    db.refresh(policy)
    response.headers["ETag"] = gate_policy_etag(policy)
    return serialize_gate_policy(policy)


@router.get("/evaluations/{evaluation_id}/gate")
def get_evaluation_gate(
    evaluation_id: uuid.UUID,
    policy_id: uuid.UUID | None = Query(default=None),
    baseline_evaluation_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """对照配置策略判定 Evaluation；不改变 Run 或评分任务状态。"""
    return evaluate_gate(
        db,
        evaluation_id=evaluation_id,
        policy_id=policy_id,
        baseline_evaluation_id=baseline_evaluation_id,
    )
